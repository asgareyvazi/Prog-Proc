"""Lessons, best practices and recommendations - what a field learns, and who is allowed to say so.

Three related records with three different standards of proof, which is the whole reason they are
three tables:

*   A **lesson** is a claim about what happened and what it taught.  Anyone who was there may write one,
    so the write is cheap and the *approval* is the expensive part: it requires evidence and a second
    person (see :meth:`LessonRepository.approve`).
*   A **best practice** is a lesson that survived contact with more than one well.  It is only created
    out of an approved lesson, and it inherits the lesson's evidence rather than asserting new facts.
*   A **recommendation** is advice the platform derived from records.  It is never a decision: it
    starts ``PROPOSED``, and only a person can move it (``ACCEPTED``, ``DECLINED`` with a reason,
    ``IMPLEMENTED`` when it has reached a procedure or a program).

The rules that make those distinctions real:

*   **No evidence, no approval.**  A lesson is approved only when it points at something - a provenance
    entry on the row, or an edge to a knowledge item, document version, event, NPT row or problem.  The
    alternative is a library of plausible-sounding folklore, which is the failure mode every lessons
    database in this industry has.
*   **You cannot approve your own lesson.**  Self-approval is refused with the reason, not silently
    allowed: ``approved_by == created_by`` is exactly the entry a reader would later trust least, and
    the platform should not be the thing that makes it.
*   **A recommendation's evidence is stored with it.**  ``evidence`` and ``query`` are columns so a
    person reading "switch to a closer bit torque limit" six months later can see the four events it
    came from and re-run the query that produced it - which is what turns "the tool said so" into
    something an engineer can argue with.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..core.enums import (
    CauseStatus,
    KnowledgeOrigin,
    KnowledgeRelationType,
    LessonLifecycle,
    ProcedureLifecycle,
    RecommendationLifecycle,
)
from ..core.errors import ValidationError
from ..core.hashing import sha256_obj
from ..core.ids import new_id
from ..core.lifecycle import (
    LESSON_LIFECYCLE,
    PROCEDURE_LIFECYCLE,
    RECOMMENDATION_LIFECYCLE,
)
from ..core.vocabulary import problem_type as match_problem_type
from ..core.vocabulary import snake_token
from ..database.integrity import create_knowledge_relation
from ..database.models import (
    BestPractice,
    DdrReport,
    DocumentVersion,
    DrillingProgram,
    Field,
    FieldPattern,
    KnowledgeItem,
    KnowledgeRelation,
    LessonLearned,
    NptRecord,
    ProblemOccurrence,
    ProcedureRecord,
    Project,
    Recommendation,
    RiskRecord,
    Well,
    WellEvent,
    WellOperation,
    WellSection,
)
from ..operations.repository import set_record_status

__all__ = ["LESSON_FIELDS", "PRACTICE_FIELDS", "LessonRepository"]

#: What a caller may set when capturing or revising a lesson.  The approval columns and the chain
#: columns are written only by the methods that validate them.
LESSON_FIELDS: frozenset[str] = frozenset(
    {
        "code",
        "title",
        "problem_type",
        "context",
        "observation",
        "root_cause",
        "root_cause_status",
        "lesson",
        "recommendation",
        "project_id",
        "field_id",
        "well_id",
        "section_id",
        "applicable_operations",
        "applicable_formations",
        "hole_size_in",
        "depth_from_value",
        "depth_from_unit",
        "depth_to_value",
        "depth_to_unit",
        "conditions",
        "confidence",
        "provenance",
        "attributes",
    }
)
PRACTICE_FIELDS: frozenset[str] = frozenset(
    {
        "code",
        "title",
        "practice_type",
        "statement",
        "rationale",
        "owner",
        "project_id",
        "field_id",
        "well_id",
        "section_id",
        "applicable_operations",
        "applicable_formations",
        "hole_size_in",
        "conditions",
        "not_applicable_when",
        "document_id",
        "document_version_id",
        "provenance",
        "attributes",
    }
)
#: The list-shaped columns, so a caller may pass any iterable and get a plain list stored.
_LIST_FIELDS: frozenset[str] = frozenset({"applicable_operations", "applicable_formations"})


def _list(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, Sequence):
        return [str(item).strip() for item in value if str(item or "").strip()]
    raise ValidationError("expected a list of words", value=repr(value)[:120])


def _prose(value: object) -> str:
    """A lesson's conditions are prose, and a mapping is refused rather than stringified.

    ``lesson_learned.conditions`` is a text column while ``best_practice.conditions`` is JSON: a lesson
    records "below the last casing shoe, in the reactive shales" in the writer's words, and a practice
    records something a screen can filter on.  Silently ``str()``-ing a caller's dict would store a
    Python repr that reads badly and never matches a query - so the mismatch is named instead, with the
    column that *is* structured.
    """
    if value is None:
        return ""
    if isinstance(value, Mapping):
        raise ValidationError(
            "a lesson's conditions are prose; structured conditions belong in attributes",
            keys=sorted(str(key) for key in value),
        )
    return str(value)


def _cause_state(value: object, text: object) -> str:
    """The stated/known/unknown state of a root cause, consistent with the operational records.

    One rule for both layers on purpose: a cause that is claimed without the cause itself written down
    is the single most common way a record looks more certain than it is, and it should fail the same
    way whether it came from a CSV or from an engineer.
    """
    stated = str(getattr(value, "value", value) or "").strip()
    body = str(text or "").strip()
    if stated and body:
        return str(CauseStatus.parse(stated))
    if body:
        return str(CauseStatus.KNOWN)
    if stated == str(CauseStatus.KNOWN):
        raise ValidationError(
            "root_cause_status is KNOWN but the root cause is empty",
            hint="write the cause, or record it as UNKNOWN or INFERRED",
        )
    return str(CauseStatus.UNKNOWN)


class LessonRepository:
    """Capture, evidence, approve, promote - and the derived advice nobody has accepted yet."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- scope ----------------------------------------------------------------
    def _check_scope(self, **scope: str) -> None:
        well_id = str(scope.get("well_id") or "")
        field_id = str(scope.get("field_id") or "")
        project_id = str(scope.get("project_id") or "")
        section_id = str(scope.get("section_id") or "")
        if section_id:
            section = self.session.get(WellSection, section_id)
            if section is None:
                raise ValidationError(f"no section {section_id!r}")
            if well_id and str(section.well_id) != well_id:
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
                        f"well {well.name} is not in {label} {wanted!r}", actual=stored
                    )
        if field_id and self.session.get(Field, field_id) is None:
            raise ValidationError(f"no field {field_id!r}")
        if project_id and self.session.get(Project, project_id) is None:
            raise ValidationError(f"no project {project_id!r}")

    # -- lessons --------------------------------------------------------------
    def capture(
        self,
        *,
        lesson: str,
        title: str = "",
        observation: str = "",
        recommendation: str = "",
        context: str = "",
        root_cause: str = "",
        root_cause_status: CauseStatus | str | None = None,
        problem_type: str = "",
        code: str = "",
        confidence: float | None = None,
        created_by: str = "system",
        origin: str = KnowledgeOrigin.MANUAL.value,
        provenance: Sequence[Mapping[str, Any]] | None = None,
        attributes: Mapping[str, Any] | None = None,
        **scope: Any,
    ) -> LessonLearned:
        """Write a lesson in ``DRAFT`` - anybody who was there can do this.

        The cheap write is deliberate.  A platform that makes capture hard gets no captures, and the
        thing that has to be hard is the transition to ``APPROVED``, which is where the evidence and the
        second reviewer are demanded.  ``lesson`` is the only required field: what was learned, in the
        writer's own words; the context, observation and cause are what makes it checkable later.
        """
        if not str(lesson or "").strip():
            raise ValidationError(
                "a lesson needs the lesson itself",
                hint="say what to do differently, not only what went wrong",
            )
        scope_keys = ("well_id", "field_id", "project_id", "section_id")
        self._check_scope(**{key: str(scope.get(key) or "") for key in scope_keys})
        accepted = {
            *scope_keys,
            "applicable_operations",
            "applicable_formations",
            "hole_size_in",
            "depth_from_value",
            "depth_from_unit",
            "depth_to_value",
            "depth_to_unit",
            "conditions",
            "reviewer",
        }
        for key in sorted(set(scope) - accepted):
            raise ValidationError(f"lesson has no field named {key}")
        row = LessonLearned(
            id=new_id("les"),
            code=str(code or "").strip() or None,
            title=str(title or "").strip()[:400] or _title_from(lesson),
            problem_type=match_problem_type(problem_type).token,
            lesson=str(lesson).strip(),
            observation=str(observation or ""),
            context=str(context or ""),
            recommendation=str(recommendation or ""),
            root_cause=str(root_cause or ""),
            root_cause_status=_cause_state(root_cause_status, root_cause),
            status=str(LESSON_LIFECYCLE.initial),
            revision=1,
            is_current=True,
            created_by=str(created_by or "") or "system",
            reviewer=str(scope.get("reviewer") or "") or None,
            project_id=str(scope.get("project_id") or "") or None,
            field_id=str(scope.get("field_id") or "") or None,
            well_id=str(scope.get("well_id") or "") or None,
            section_id=str(scope.get("section_id") or "") or None,
            applicable_operations=_list(scope.get("applicable_operations")),
            applicable_formations=_list(scope.get("applicable_formations")),
            hole_size_in=scope.get("hole_size_in"),
            depth_from_value=scope.get("depth_from_value"),
            depth_from_unit=str(scope.get("depth_from_unit") or "") or None,
            depth_to_value=scope.get("depth_to_value"),
            depth_to_unit=str(scope.get("depth_to_unit") or "") or None,
            conditions=_prose(scope.get("conditions")),
            confidence=None if confidence is None else float(confidence),
            provenance=[dict(item) for item in provenance or ()],
            origin=str(getattr(origin, "value", origin)),
            attributes=dict(attributes or {}),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_lesson(self, lesson_id: str) -> LessonLearned:
        row = self.session.get(LessonLearned, str(lesson_id))
        if row is None:
            raise ValidationError(f"no lesson {lesson_id!r}")
        return row

    def update_lesson(self, lesson_id: str, **values: Any) -> tuple[LessonLearned, dict[str, Any]]:
        """Change a lesson that has not been approved yet, and report what moved.

        An approved lesson is edited by superseding it (:meth:`revise`), never in place: the approval is
        about the text that was, and rewriting that text underneath the approval is how a library of
        "lessons" ends up containing sentences nobody reviewed.
        """
        row = self.get_lesson(lesson_id)
        if str(row.status) == str(LessonLifecycle.APPROVED):
            raise ValidationError(
                "an approved lesson is changed by a new revision, not in place",
                hint="use revise() so the approved text stays readable",
            )
        unknown = sorted(set(values) - LESSON_FIELDS)
        if unknown:
            raise ValidationError(
                f"lesson has no field named {', '.join(unknown)}", allowed=sorted(LESSON_FIELDS)
            )
        applied: dict[str, Any] = {}
        for field, value in values.items():
            if field in _LIST_FIELDS:
                payload: Any = _list(value)
            elif field == "problem_type":
                payload = match_problem_type(value).token
            elif field == "conditions":
                payload = _prose(value)
            elif field == "provenance":
                payload = [dict(item) for item in value or ()]
            elif field == "attributes":
                payload = dict(value or {})
            elif field == "lesson":
                payload = str(value or "").strip()
                if not payload:
                    raise ValidationError("a lesson cannot be emptied")
            elif field == "root_cause":
                payload = str(value or "")
                row.root_cause_status = _cause_state(row.root_cause_status, payload)
            elif field == "confidence":
                payload = None if value is None else float(value)
            else:
                payload = value
            setattr(row, field, payload)
            applied[field] = payload
        self.session.flush()
        return row, applied

    def revise(
        self,
        lesson_id: str,
        *,
        by: str,
        changes: Mapping[str, Any] | None = None,
        revision_label: str = "",
    ) -> LessonLearned:
        """Supersede a lesson with a new revision, carrying its evidence links to the new row.

        The evidence edges are *re-pointed* rather than copied: a lesson's proof does not change when
        its wording is tightened, and leaving the edges on the superseded row would make the current
        revision look unbacked - which is precisely the shape that gets a good lesson ignored.
        """
        source = self.get_lesson(lesson_id)
        if not source.is_current:
            raise ValidationError(
                "only the current revision of a lesson can be superseded",
                current_revision=source.revision,
            )
        if not str(by or "").strip():
            raise ValidationError("a revision needs an author", hint="pass by=<who revised it>")
        payload = dict(changes or {})
        unknown = sorted(set(payload) - LESSON_FIELDS)
        if unknown:
            raise ValidationError(
                f"lesson has no field named {', '.join(unknown)}", allowed=sorted(LESSON_FIELDS)
            )
        row = LessonLearned(
            id=new_id("les"),
            code=source.code,
            title=str(payload.get("title") or source.title)[:400],
            problem_type=str(payload.get("problem_type") or source.problem_type or ""),
            lesson=str(payload.get("lesson") or source.lesson),
            observation=str(payload.get("observation", source.observation) or ""),
            context=str(payload.get("context", source.context) or ""),
            recommendation=str(payload.get("recommendation", source.recommendation) or ""),
            root_cause=str(payload.get("root_cause", source.root_cause) or ""),
            root_cause_status=_cause_state(
                payload.get("root_cause_status") or source.root_cause_status,
                payload.get("root_cause", source.root_cause),
            ),
            status=str(LessonLifecycle.DRAFT),
            revision=int(source.revision or 1) + 1,
            revision_label=str(revision_label or payload.get("revision_label") or "") or None,
            is_current=True,
            supersedes_id=source.id,
            created_by=by,
            project_id=source.project_id,
            field_id=source.field_id,
            well_id=source.well_id,
            section_id=source.section_id,
            applicable_operations=_list(
                payload.get("applicable_operations", source.applicable_operations)
            ),
            applicable_formations=_list(
                payload.get("applicable_formations", source.applicable_formations)
            ),
            hole_size_in=payload.get("hole_size_in", source.hole_size_in),
            depth_from_value=payload.get("depth_from_value", source.depth_from_value),
            depth_from_unit=source.depth_from_unit,
            depth_to_value=payload.get("depth_to_value", source.depth_to_value),
            depth_to_unit=source.depth_to_unit,
            conditions=_prose(payload.get("conditions", source.conditions)),
            confidence=payload.get("confidence", source.confidence),
            provenance=(
                [dict(item) for item in payload["provenance"]]
                if payload.get("provenance") is not None
                else list(source.provenance or [])
            ),
            origin=str(source.origin),
            attributes=dict(payload.get("attributes") or source.attributes or {}),
        )
        source.is_current = False
        # Superseding is mechanical (see the note in the engineering repository): a lesson that was
        # approved stays readable as approved-but-superseded, and a draft that was rewritten simply
        # stops being current.
        source.status = str(LessonLifecycle.SUPERSEDED)
        self.session.add(row)
        self.session.flush()
        self._move_evidence(source, row)
        return row

    def _move_evidence(self, source: LessonLearned, row: LessonLearned) -> int:
        """Re-point every evidence and outcome edge from the superseded lesson to the new one."""
        moved = 0
        for edge in self.session.execute(
            select(KnowledgeRelation).where(
                KnowledgeRelation.source_type == "lesson",
                KnowledgeRelation.source_id == source.id,
            )
        ).scalars():
            edge.source_id = row.id
            moved += 1
        for edge in self.session.execute(
            select(KnowledgeRelation).where(
                KnowledgeRelation.target_type == "lesson",
                KnowledgeRelation.target_id == source.id,
            )
        ).scalars():
            edge.target_id = row.id
            moved += 1
        self.session.flush()
        return moved

    # -- evidence -------------------------------------------------------------
    def attach_evidence(
        self,
        lesson_id: str,
        *,
        knowledge_item_ids: Sequence[str] = (),
        document_version_ids: Sequence[str] = (),
        event_ids: Sequence[str] = (),
        npt_ids: Sequence[str] = (),
        problem_ids: Sequence[str] = (),
        operation_ids: Sequence[str] = (),
        report_ids: Sequence[str] = (),
        well_ids: Sequence[str] = (),
        note: str = "",
    ) -> dict[str, int]:
        """Attach the records this lesson rests on, as edges in the one knowledge graph.

        Two relation types are used, and the difference is not cosmetic:
        ``LESSON_CITES_EVIDENCE`` says "this document supports what I wrote", while
        ``LESSON_DERIVED_FROM_EVENT`` / ``LESSON_DERIVED_FROM_WELL`` says "this happened, and that is
        where the lesson came from".  A reader deciding whether to change their program cares which of
        the two a lesson has, and a graph that recorded only "related" would not be able to tell them.
        """
        row = self.get_lesson(lesson_id)
        counts: dict[str, int] = {"cited": 0, "derived": 0}
        for item_id in knowledge_item_ids:
            if self.session.get(KnowledgeItem, str(item_id)) is None:
                raise ValidationError(f"no knowledge item {item_id!r}")
            self._edge(
                row,
                KnowledgeRelationType.LESSON_CITES_EVIDENCE,
                "knowledge_item",
                str(item_id),
                note,
            )
            counts["cited"] += 1
        for version_id in document_version_ids:
            if self.session.get(DocumentVersion, str(version_id)) is None:
                raise ValidationError(f"no document version {version_id!r}")
            self._edge(
                row,
                KnowledgeRelationType.LESSON_CITES_EVIDENCE,
                "document_version",
                str(version_id),
                note,
            )
            counts["cited"] += 1
        for label, model, relation, ids in (
            ("event", WellEvent, KnowledgeRelationType.LESSON_DERIVED_FROM_EVENT, event_ids),
            ("npt record", NptRecord, KnowledgeRelationType.LESSON_DERIVED_FROM_EVENT, npt_ids),
            (
                "problem",
                ProblemOccurrence,
                KnowledgeRelationType.LESSON_DERIVED_FROM_EVENT,
                problem_ids,
            ),
            (
                "operation",
                WellOperation,
                KnowledgeRelationType.LESSON_DERIVED_FROM_EVENT,
                operation_ids,
            ),
            ("report", DdrReport, KnowledgeRelationType.LESSON_DERIVED_FROM_EVENT, report_ids),
        ):
            for row_id in ids:
                if self.session.get(model, str(row_id)) is None:
                    raise ValidationError(f"no {label} {row_id!r}")
                self._edge(row, relation, _ENDPOINT_NAMES[model.__tablename__], str(row_id), note)
                counts["derived"] += 1
        for well_id in well_ids:
            if self.session.get(Well, str(well_id)) is None:
                raise ValidationError(f"no well {well_id!r}")
            self._edge(
                row,
                KnowledgeRelationType.LESSON_DERIVED_FROM_WELL,
                "well",
                str(well_id),
                note or "the well this lesson was learnt on",
            )
            counts["derived"] += 1
        return counts

    def _edge(
        self,
        lesson: LessonLearned,
        relation: KnowledgeRelationType,
        target_type: str,
        target_id: str,
        note: str,
    ) -> None:
        create_knowledge_relation(
            self.session,
            source_type="lesson",
            source_id=lesson.id,
            relation=relation.value,
            target_type=target_type,
            target_id=target_id,
            provenance=list(lesson.provenance or [])[:1],
            note=str(note or "") or relation.value.lower(),
        )

    def evidence(self, lesson_id: str) -> dict[str, list[dict[str, Any]]]:
        """What a lesson rests on, split the way the edges were written.

        Read straight out of the graph rather than from a column on the lesson, because the evidence is
        the thing most likely to change on its own (a fact retired, a document superseded) and a copy
        would silently disagree with the row it describes.
        """
        row = self.get_lesson(lesson_id)
        payload: dict[str, list[dict[str, Any]]] = {"cited": [], "derived": [], "wells": []}
        for edge in self.session.execute(
            select(KnowledgeRelation)
            .where(KnowledgeRelation.source_type == "lesson", KnowledgeRelation.source_id == row.id)
            .order_by(KnowledgeRelation.relation, KnowledgeRelation.target_id)
        ).scalars():
            entry = {
                "relation": edge.relation,
                "target_type": edge.target_type,
                "target_id": edge.target_id,
                "note": edge.note,
            }
            if edge.relation == KnowledgeRelationType.LESSON_CITES_EVIDENCE.value:
                payload["cited"].append(entry)
            elif edge.relation == KnowledgeRelationType.LESSON_DERIVED_FROM_WELL.value:
                payload["wells"].append(entry)
            else:
                payload["derived"].append(entry)
        return payload

    def evidence_count(self, lesson_id: str) -> int:
        """Every piece of evidence the lesson has: edges plus the provenance on its own row."""
        row = self.get_lesson(lesson_id)
        edges = self.evidence(lesson_id)
        return (
            len(edges["cited"])
            + len(edges["derived"])
            + len(edges["wells"])
            + len(row.provenance or [])
        )

    # -- review ---------------------------------------------------------------
    def submit_for_review(self, lesson_id: str, *, by: str = "") -> LessonLearned:
        row = self.get_lesson(lesson_id)
        set_record_status(
            self.session, row, LessonLifecycle.REVIEW, by=by, lifecycle=LESSON_LIFECYCLE
        )
        if str(row.reviewer or "") == "" and str(by or "").strip():
            row.reviewer = by
            self.session.flush()
        return row

    def approve(self, lesson_id: str, *, by: str, note: str = "") -> LessonLearned:
        """Accept a lesson - which needs evidence and a reviewer who is not the author.

        Both refusals are the point of the method.  An unbacked lesson in the library is worse than no
        lesson, because it will be quoted; and an author-approved lesson is a record of nobody having
        checked it, which is exactly what a program review will want to know.
        """
        row = self.get_lesson(lesson_id)
        if not str(by or "").strip():
            raise ValidationError("an approval needs an approver", hint="pass by=<who accepted it>")
        if str(row.created_by or "") and str(row.created_by) == str(by):
            raise ValidationError(
                "the author of a lesson cannot approve it",
                hint="ask someone who owns the operation to review it",
                created_by=row.created_by,
            )
        if self.evidence_count(row.id) == 0:
            raise ValidationError(
                "a lesson with no evidence cannot be approved",
                hint="attach the event, NPT row, document version or knowledge item it came from",
                lesson_id=row.id,
            )
        set_record_status(
            self.session,
            row,
            LessonLifecycle.APPROVED,
            by=by,
            reason=note,
            lifecycle=LESSON_LIFECYCLE,
        )
        row.approved_by = by
        row.approved_at = datetime.now(UTC)
        row.reviewer = row.reviewer or by
        row.reviewed_at = row.reviewed_at or row.approved_at
        self.session.flush()
        return row

    def reject(self, lesson_id: str, *, by: str, reason: str) -> LessonLearned:
        """Refuse a lesson, with the reason kept on the row.

        A rejection with a reason is a usable answer to "did anybody think about this"; a silent one is
        indistinguishable from neglect, and the second time the same lesson is captured somebody will
        spend the same effort on it.
        """
        row = self.get_lesson(lesson_id)
        if not str(reason or "").strip():
            raise ValidationError(
                "rejecting a lesson needs a reason",
                hint="pass reason=<why this does not hold up>",
            )
        set_record_status(
            self.session,
            row,
            LessonLifecycle.REJECTED,
            by=by,
            reason=reason,
            lifecycle=LESSON_LIFECYCLE,
        )
        return row

    def reopen(self, lesson_id: str, *, by: str, reason: str = "") -> LessonLearned:
        """Put a rejected lesson back to ``DRAFT`` so it can be fixed and re-submitted."""
        row = self.get_lesson(lesson_id)
        set_record_status(
            self.session,
            row,
            LessonLifecycle.DRAFT,
            by=by,
            reason=reason,
            lifecycle=LESSON_LIFECYCLE,
        )
        return row

    # -- reading --------------------------------------------------------------
    def list_lessons(
        self,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
        section_id: str = "",
        problem_type: str = "",
        status: str = "",
        approved_only: bool = False,
        include_superseded: bool = False,
        search: str = "",
        limit: int = 200,
    ) -> list[LessonLearned]:
        statement = select(LessonLearned)
        if well_id or section_id:
            statement = statement.where(
                LessonLearned.well_id == well_id
                if well_id
                else LessonLearned.section_id == section_id
            )
        for label, value in (("field_id", field_id), ("project_id", project_id)):
            if value:
                scoped_wells = select(Well.id).where(getattr(Well, label) == value)
                statement = statement.where(
                    or_(
                        getattr(LessonLearned, label) == value,
                        LessonLearned.well_id.in_(scoped_wells),
                    )
                )
        if problem_type:
            statement = statement.where(
                LessonLearned.problem_type == match_problem_type(problem_type).token
            )
        if status:
            statement = statement.where(LessonLearned.status == str(LESSON_LIFECYCLE.parse(status)))
        elif approved_only:
            statement = statement.where(LessonLearned.status == str(LessonLifecycle.APPROVED))
        if not include_superseded:
            statement = statement.where(LessonLearned.is_current.is_(True))
        if str(search or "").strip():
            pattern = f"%{str(search).strip().lower()}%"
            statement = statement.where(
                or_(
                    func.lower(LessonLearned.title).like(pattern),
                    func.lower(LessonLearned.lesson).like(pattern),
                    func.lower(LessonLearned.observation).like(pattern),
                )
            )
        return list(
            self.session.execute(
                statement.order_by(
                    LessonLearned.approved_at.desc().nulls_last(),
                    LessonLearned.created_at.desc(),
                    LessonLearned.id,
                ).limit(_bounded(limit))
            ).scalars()
        )

    def lessons_for_well(self, well_id: str, *, approved_only: bool = True) -> list[LessonLearned]:
        """Lessons written for this well, plus its field's, so a plan can be checked against both."""
        well = self.session.get(Well, str(well_id))
        if well is None:
            raise ValidationError(f"no well {well_id!r}")
        scopes = [LessonLearned.well_id == well.id]
        for label, value in (("field_id", well.field_id), ("project_id", well.project_id)):
            if value:
                scopes.append(getattr(LessonLearned, label) == value)
        statement = select(LessonLearned).where(LessonLearned.is_current.is_(True), or_(*scopes))
        if approved_only:
            statement = statement.where(LessonLearned.status == str(LessonLifecycle.APPROVED))
        return list(
            self.session.execute(
                statement.order_by(LessonLearned.approved_at.desc().nulls_last(), LessonLearned.id)
            ).scalars()
        )

    # -- best practices -------------------------------------------------------
    def promote_to_best_practice(
        self,
        lesson_id: str,
        *,
        by: str,
        statement: str = "",
        rationale: str = "",
        practice_type: str = "general",
        code: str = "",
        title: str = "",
        owner: str = "",
        not_applicable_when: str = "",
        applicable_operations: Sequence[str] = (),
        applicable_formations: Sequence[str] = (),
        hole_size_in: float | None = None,
        conditions: Mapping[str, Any] | None = None,
    ) -> BestPractice:
        """Turn an approved lesson into a field-level practice.

        A practice is created at ``DRAFT`` and still needs its own approval: the lesson being accepted
        means "this is what we learnt", not "this is how every well here is drilled".  ``statement``
        defaults to the lesson's own wording, and the provenance and evidence edges come with it, so the
        practice can be traced back to the events that produced it instead of standing on its own
        authority.

        The lesson's own prose conditions are deliberately not copied into the practice's JSON
        ``conditions``: a filter that had to be built by parsing a sentence is a filter nobody can
        trust, and the lesson stays one edge away for anybody who wants to read what it said.
        """
        lesson = self.get_lesson(lesson_id)
        if str(lesson.status) != str(LessonLifecycle.APPROVED):
            raise ValidationError(
                "only an approved lesson can become a best practice",
                hint=f"this lesson is {lesson.status}",
                status=lesson.status,
            )
        if not str(by or "").strip():
            raise ValidationError(
                "a practice needs an owner", hint="pass by=<who is publishing it>"
            )
        text = str(statement or "").strip() or str(lesson.lesson or "").strip()
        if not text:
            raise ValidationError("a best practice needs a statement")
        row = BestPractice(
            id=new_id("bp"),
            code=str(code or "").strip() or None,
            title=str(title or "").strip()[:400] or _title_from(text),
            practice_type=_token(practice_type),
            statement=text,
            rationale=str(rationale or "") or str(lesson.observation or ""),
            revision=1,
            is_current=True,
            status=str(ProcedureLifecycle.DRAFT),
            owner=str(owner or "") or str(by),
            project_id=lesson.project_id,
            field_id=lesson.field_id,
            well_id=None,  # a practice is deliberately not well-scoped: that is what it means
            section_id=None,
            applicable_operations=_list(applicable_operations)
            or _list(lesson.applicable_operations),
            applicable_formations=_list(applicable_formations)
            or _list(lesson.applicable_formations),
            hole_size_in=hole_size_in if hole_size_in is not None else lesson.hole_size_in,
            conditions=dict(conditions or {}),
            not_applicable_when=str(not_applicable_when or ""),
            provenance=list(lesson.provenance or []),
            origin=str(lesson.origin),
            created_by=by,
            attributes={
                "promoted_from_lesson": lesson.id,
                "lesson_revision": lesson.revision,
            },
        )
        self.session.add(row)
        self.session.flush()
        create_knowledge_relation(
            self.session,
            source_type="lesson",
            source_id=lesson.id,
            relation=KnowledgeRelationType.LESSON_BEST_PRACTICE.value,
            target_type="best_practice",
            target_id=row.id,
            note="the practice this lesson became",
        )
        return row

    def get_practice(self, practice_id: str) -> BestPractice:
        row = self.session.get(BestPractice, str(practice_id))
        if row is None:
            raise ValidationError(f"no best practice {practice_id!r}")
        return row

    def update_practice(
        self, practice_id: str, **values: Any
    ) -> tuple[BestPractice, dict[str, Any]]:
        row = self.get_practice(practice_id)
        if str(row.status) == str(ProcedureLifecycle.APPROVED):
            raise ValidationError(
                "an approved practice is changed by a new revision, not in place",
                hint="use revise_practice()",
            )
        unknown = sorted(set(values) - PRACTICE_FIELDS)
        if unknown:
            raise ValidationError(
                f"best practice has no field named {', '.join(unknown)}",
                allowed=sorted(PRACTICE_FIELDS),
            )
        applied: dict[str, Any] = {}
        for field, value in values.items():
            if field in _LIST_FIELDS:
                payload: Any = _list(value)
            elif field == "practice_type":
                payload = _token(value)
            elif field == "statement":
                payload = str(value or "").strip()
                if not payload:
                    raise ValidationError("a best practice cannot be emptied")
            elif field in {"conditions", "attributes"}:
                payload = dict(value or {})
            elif field == "provenance":
                payload = [dict(item) for item in value or ()]
            else:
                payload = value
            setattr(row, field, payload)
            applied[field] = payload
        self.session.flush()
        return row, applied

    def revise_practice(
        self,
        practice_id: str,
        *,
        by: str,
        changes: Mapping[str, Any] | None = None,
        revision_label: str = "",
    ) -> BestPractice:
        """Supersede a practice with a new revision, same rules as a procedure."""
        source = self.get_practice(practice_id)
        if not source.is_current:
            raise ValidationError(
                "only the current revision of a practice can be superseded",
                current_revision=source.revision,
            )
        if not str(by or "").strip():
            raise ValidationError("a revision needs an author", hint="pass by=<who revised it>")
        payload = dict(changes or {})
        unknown = sorted(set(payload) - PRACTICE_FIELDS)
        if unknown:
            raise ValidationError(
                f"best practice has no field named {', '.join(unknown)}",
                allowed=sorted(PRACTICE_FIELDS),
            )
        row = BestPractice(
            id=new_id("bp"),
            code=source.code,
            title=str(payload.get("title") or source.title)[:400],
            practice_type=_token(payload.get("practice_type") or source.practice_type),
            statement=str(payload.get("statement") or source.statement),
            rationale=str(payload.get("rationale", source.rationale) or ""),
            revision=int(source.revision or 1) + 1,
            revision_label=str(revision_label or payload.get("revision_label") or "") or None,
            is_current=True,
            supersedes_id=source.id,
            status=str(ProcedureLifecycle.DRAFT),
            owner=str(payload.get("owner") or source.owner or "") or None,
            project_id=source.project_id,
            field_id=source.field_id,
            well_id=None,
            section_id=None,
            applicable_operations=_list(
                payload.get("applicable_operations", source.applicable_operations)
            ),
            applicable_formations=_list(
                payload.get("applicable_formations", source.applicable_formations)
            ),
            hole_size_in=payload.get("hole_size_in", source.hole_size_in),
            conditions=dict(payload.get("conditions") or source.conditions or {}),
            not_applicable_when=str(
                payload.get("not_applicable_when", source.not_applicable_when) or ""
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
        source.status = str(ProcedureLifecycle.SUPERSEDED)
        self.session.add(row)
        self.session.flush()
        return row

    def approve_practice(self, practice_id: str, *, by: str, note: str = "") -> BestPractice:
        """Approve a practice: a second person, and a rationale that is not empty.

        The rationale requirement is the difference between a best practice and a house rule.  "Use a
        closer bit torque limit" is a sentence; the same sentence with "because three washouts in this
        field started at 28k ft-lb" attached is something a driller can adapt when the situation changes.
        """
        row = self.get_practice(practice_id)
        if not str(by or "").strip():
            raise ValidationError("an approval needs an approver")
        if str(row.created_by or "") == str(by):
            raise ValidationError(
                "the author of a practice cannot approve it",
                created_by=row.created_by,
                hint="a practice needs somebody else to accept it",
            )
        if not str(row.rationale or "").strip():
            raise ValidationError(
                "a best practice needs a rationale before it can be approved",
                hint="say why, not only what",
            )
        set_record_status(
            self.session,
            row,
            ProcedureLifecycle.APPROVED,
            by=by,
            reason=note,
            lifecycle=PROCEDURE_LIFECYCLE,
        )
        row.approved_by = by
        row.approved_at = datetime.now(UTC)
        row.reviewer = row.reviewer or by
        row.reviewed_at = row.reviewed_at or row.approved_at
        self.session.flush()
        return row

    def list_practices(
        self,
        *,
        field_id: str = "",
        project_id: str = "",
        practice_type: str = "",
        status: str = "",
        include_superseded: bool = False,
        limit: int = 200,
    ) -> list[BestPractice]:
        statement = select(BestPractice)
        if field_id:
            statement = statement.where(
                or_(
                    BestPractice.field_id == field_id,
                    BestPractice.well_id.in_(select(Well.id).where(Well.field_id == field_id)),
                )
            )
        if project_id:
            statement = statement.where(
                or_(
                    BestPractice.project_id == project_id,
                    BestPractice.well_id.in_(select(Well.id).where(Well.project_id == project_id)),
                )
            )
        if practice_type:
            statement = statement.where(BestPractice.practice_type == _token(practice_type))
        if status:
            statement = statement.where(
                BestPractice.status == str(PROCEDURE_LIFECYCLE.parse(status))
            )
        if not include_superseded:
            statement = statement.where(BestPractice.is_current.is_(True))
        return list(
            self.session.execute(
                statement.order_by(BestPractice.code, BestPractice.revision.desc()).limit(
                    max(0, int(limit))
                )
            ).scalars()
        )

    def practices_for_well(
        self, well_id: str, *, hole_size_in: float | None = None
    ) -> list[BestPractice]:
        """The approved practices that govern this well's field, optionally for one hole size.

        ``hole_size_in`` is the filter the column exists for: a practice written for the 8½ in section
        should not clutter a surface-hole plan.  A practice with no hole size recorded is applicable to
        every hole - that is what "no range" means here, and it is why the column is nullable rather than
        defaulted.  Depth filtering is deliberately absent: the practice table has no depth columns, and
        the lesson it came from keeps those, so an answer that needed a depth is asked of
        :meth:`lessons_for_well` instead of pretending the range survived promotion.
        """
        well = self.session.get(Well, str(well_id))
        if well is None:
            raise ValidationError(f"no well {well_id!r}")
        scopes = []
        if well.field_id:
            scopes.append(BestPractice.field_id == well.field_id)
        if well.project_id:
            scopes.append(BestPractice.project_id == well.project_id)
        if not scopes:
            return []
        rows = list(
            self.session.execute(
                select(BestPractice)
                .where(
                    BestPractice.is_current.is_(True),
                    BestPractice.status == str(ProcedureLifecycle.APPROVED),
                    or_(*scopes),
                )
                .order_by(BestPractice.code, BestPractice.title)
            ).scalars()
        )
        if hole_size_in is None:
            return rows
        wanted = float(hole_size_in)
        return [
            row
            for row in rows
            if row.hole_size_in is None or abs(float(row.hole_size_in) - wanted) <= 0.05
        ]

    # -- recommendations ------------------------------------------------------
    def propose_recommendation(
        self,
        *,
        statement: str,
        reason: str = "",
        generated_by: str = "intelligence",
        confidence: float | None = None,
        evidence: Sequence[Mapping[str, Any]] | None = None,
        query: Mapping[str, Any] | None = None,
        applicability: Mapping[str, Any] | None = None,
        lesson_id: str = "",
        practice_id: str = "",
        pattern_id: str = "",
        problem_id: str = "",
        risk_id: str = "",
        procedure_id: str = "",
        program_id: str = "",
        attributes: Mapping[str, Any] | None = None,
        **scope: Any,
    ) -> Recommendation:
        """Write down advice the platform derived from records, as a row a person can decide on.

        The signature is a digest of the statement and the scope, which is what makes the whole proposal
        path repeatable: re-running the analysis that produced this advice either finds the same
        recommendation (and the existing row is returned, decision and all) or a genuinely different
        one.  Without it, every recomputation would either duplicate open advice or - worse, if rows
        were blindly replaced - throw away a person's decline.
        """
        if not str(statement or "").strip():
            raise ValidationError("a recommendation needs a statement")
        unknown = sorted(
            set(scope) - {"project_id", "field_id", "well_id", "section_id", "operation_id"}
        )
        if unknown:
            raise ValidationError(
                f"recommendation has no scope field named {', '.join(unknown)}",
                allowed=sorted({"project_id", "field_id", "well_id", "section_id", "operation_id"}),
            )
        for label, model in (
            ("lesson", LessonLearned),
            ("best practice", BestPractice),
            ("problem", ProblemOccurrence),
            ("risk", RiskRecord),
            ("procedure", ProcedureRecord),
            ("program", DrillingProgram),
        ):
            wanted = {
                "lesson": lesson_id,
                "best practice": practice_id,
                "problem": problem_id,
                "risk": risk_id,
                "procedure": procedure_id,
                "program": program_id,
            }[label]
            if wanted and self.session.get(model, str(wanted)) is None:
                raise ValidationError(f"no {label} {wanted!r}")
        if pattern_id and self.session.get(FieldPattern, str(pattern_id)) is None:
            raise ValidationError(f"no pattern {pattern_id!r}")
        body = str(statement).strip()
        signature_scope = {
            key: str(scope.get(key) or "") or None
            for key in ("project_id", "field_id", "well_id", "section_id", "operation_id")
        }
        signature = sha256_obj(
            {
                "statement": body,
                "scope": signature_scope,
                "lesson": lesson_id or None,
                "pattern": pattern_id or None,
                "practice": practice_id or None,
            }
        )[:32]
        existing = self.session.execute(
            select(Recommendation).where(Recommendation.signature == signature)
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = Recommendation(
            id=new_id("rec"),
            signature=signature,
            statement=body,
            reason=str(reason or ""),
            evidence=[dict(item) for item in evidence or ()],
            query=dict(query or {}),
            status=str(RECOMMENDATION_LIFECYCLE.initial),
            confidence=None if confidence is None else float(confidence),
            applicability=dict(applicability or {}),
            pattern_id=pattern_id or None,
            lesson_id=lesson_id or None,
            practice_id=practice_id or None,
            problem_id=problem_id or None,
            risk_id=risk_id or None,
            procedure_id=procedure_id or None,
            program_id=program_id or None,
            operation_id=str(scope.get("operation_id") or "") or None,
            project_id=str(scope.get("project_id") or "") or None,
            field_id=str(scope.get("field_id") or "") or None,
            well_id=str(scope.get("well_id") or "") or None,
            section_id=str(scope.get("section_id") or "") or None,
            generated_by=str(generated_by or "") or "intelligence",
            attributes=dict(attributes or {}),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_recommendation(self, recommendation_id: str) -> Recommendation:
        row = self.session.get(Recommendation, str(recommendation_id))
        if row is None:
            raise ValidationError(f"no recommendation {recommendation_id!r}")
        return row

    def decide_recommendation(
        self,
        recommendation_id: str,
        decision: RecommendationLifecycle | str,
        *,
        by: str,
        reason: str = "",
    ) -> Recommendation:
        """Accept, decline or record-as-implemented a recommendation.

        A decision is attributed and, for a decline, explained.  This is the only place where derived
        advice acquires authority: the platform proposes, and the row's ``status`` is a record of who
        said yes or no and when - never something a model gets to write.
        """
        row = self.get_recommendation(recommendation_id)
        if not str(by or "").strip():
            raise ValidationError("a decision on a recommendation needs a person", hint="pass by=")
        target = str(RECOMMENDATION_LIFECYCLE.parse(decision))
        if target == str(RecommendationLifecycle.DECLINED) and not str(reason or "").strip():
            raise ValidationError(
                "declining a recommendation needs a reason",
                hint="pass reason=<why this does not apply here>",
            )
        set_record_status(
            self.session,
            row,
            target,
            by=by,
            reason=reason,
            lifecycle=RECOMMENDATION_LIFECYCLE,
        )
        row.decided_by = by
        row.decided_at = datetime.now(UTC)
        if target == str(RecommendationLifecycle.DECLINED):
            row.decline_reason = str(reason).strip()
        self.session.flush()
        return row

    def list_recommendations(
        self,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
        status: str = "",
        lesson_id: str = "",
        pattern_id: str = "",
        limit: int = 200,
    ) -> list[Recommendation]:
        statement = select(Recommendation)
        for label, value in (
            ("well_id", well_id),
            ("field_id", field_id),
            ("project_id", project_id),
            ("lesson_id", lesson_id),
            ("pattern_id", pattern_id),
        ):
            if value:
                statement = statement.where(getattr(Recommendation, label) == value)
        if status:
            statement = statement.where(
                Recommendation.status == str(RECOMMENDATION_LIFECYCLE.parse(status))
            )
        return list(
            self.session.execute(
                statement.order_by(
                    Recommendation.created_at.desc(), Recommendation.id.desc()
                ).limit(_bounded(limit))
            ).scalars()
        )

    def counts(self, *, field_id: str = "", project_id: str = "") -> dict[str, Any]:
        """How much learning is in the library, and how much of it is backed.

        The ``without_evidence`` count is the number a knowledge librarian has to look at: a lesson with
        no proof is a liability, not an asset, and "we have 40 lessons" hides that.
        """
        statement = select(LessonLearned)
        practice_statement = select(BestPractice)
        recommendation_statement = select(Recommendation)
        for label, value in (("field_id", field_id), ("project_id", project_id)):
            if value:
                statement = statement.where(getattr(LessonLearned, label) == value)
                practice_statement = practice_statement.where(getattr(BestPractice, label) == value)
                recommendation_statement = recommendation_statement.where(
                    getattr(Recommendation, label) == value
                )
        lessons = list(self.session.execute(statement).scalars())
        by_status: dict[str, int] = {}
        without_evidence = 0
        for row in lessons:
            by_status[str(row.status)] = by_status.get(str(row.status), 0) + 1
            if not (row.provenance or []):
                without_evidence += 1
        open_recommendations = 0
        for row in self.session.execute(recommendation_statement).scalars():
            if str(row.status) == str(RecommendationLifecycle.PROPOSED):
                open_recommendations += 1
        return {
            "lessons": len(lessons),
            "by_status": by_status,
            "without_evidence": without_evidence,
            "practices": self.session.execute(
                select(func.count()).select_from(practice_statement.subquery())
            ).scalar_one(),
            "recommendations_open": open_recommendations,
        }


#: The endpoint names ``create_knowledge_relation`` knows for the models a lesson can cite.
_ENDPOINT_NAMES: dict[str, str] = {
    "well_operation": "well_operation",
    "well_event": "well_event",
    "npt_record": "npt_record",
    "problem_occurrence": "problem_occurrence",
    "ddr_report": "ddr_report",
}


def _title_from(text: str) -> str:
    """A title from the first sentence, so a capture with no title still has a readable heading.

    Truncation rather than invention: the alternative is an LLM-shaped summary, and a library title that
    nobody wrote is the beginning of a record that means something different to every reader.
    """
    first = str(text or "").strip().split(". ")[0].split("\n")[0].strip()
    return (first[:120] or "untitled lesson").rstrip(".")


def _bounded(limit: int) -> int | None:
    """A ``LIMIT`` value, with ``0`` meaning "do not limit".

    A register is paginated for a screen and unpaginated for a summary, from the same query.  Reading
    ``limit=0`` as ``LIMIT 0`` would report an empty field, which is a wrong answer to a question that
    has a right one.
    """
    return None if int(limit) <= 0 else int(limit)


def _token(value: object, fallback: str = "general") -> str:
    token = snake_token(value)
    return token or fallback
