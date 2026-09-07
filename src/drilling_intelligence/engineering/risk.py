"""Risks: the register, its evidence, and the boundary around scoring.

A risk record is a claim about the future with a name, a scope and an owner attached - which is why it
gets its own table instead of a paragraph in a program: "what are we carrying in this field, and who is
watching it" has to be queryable, and an entry buried in a PDF is neither current nor countable.

The rules that matter here:

*   **The numbers a source states are stored as stated; the platform computes none of them.**  This layer
    persists an assessment - ``probability``, ``impact``, ``severity``, ``severity_band``, ``scale`` - and
    does not invent a scoring methodology.  A matrix written here would look like the operator's own
    process while being nobody's: their corporate register, their ALARP rules, their impact definitions
    all differ, and a number derived by the tool would sit in the same column as the numbers the operator
    wrote, indistinguishable.  So a risk whose source gave a severity of 12 stores 12; one that gave the
    two axes and no product stores those and a null; one that gave nothing is unscored rather than zero.
*   **Ranges are checked, meanings are not.**  The 1..5 bounds the database enforces on the two axes are
    enforced here too, with a message that says which axis was wrong, and ``severity_band`` must be one of
    the platform's four words.  Nothing reads a number and decides what it means.
*   **The scale travels with the row.**  ``scale`` records which grid stated numbers belong to, so a
    register merged from two operators cannot silently average a 4x4 with a 5x5.
*   **An assessment without a cause is a label.**  ``causes``/``consequences`` are lists the register can
    count; ``mitigation``/``contingency`` are prose, in the owner's words.  A mitigation becomes a control
    only when it points at something, which is what :meth:`RiskRepository.link_procedure` is for.

A deterministic scoring engine can be added later as its own module.  When it is, it writes through
:meth:`RiskRepository.assess_risk` - so the row still records who or what changed an assessment - and it
must record its method and version in ``attributes`` so a score can always be traced to the arithmetic
that produced it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..core.enums import (
    KnowledgeOrigin,
    KnowledgeRelationType,
    RiskLifecycle,
    SeverityLevel,
)
from ..core.errors import ValidationError
from ..core.ids import new_id
from ..core.lifecycle import RISK_LIFECYCLE
from ..core.vocabulary import severity as match_severity
from ..core.vocabulary import snake_token
from ..database.integrity import create_knowledge_relation
from ..database.models import (
    NptRecord,
    ProblemOccurrence,
    ProcedureRecord,
    RiskRecord,
    Well,
    WellOperation,
    WellSection,
)
from ..operations.repository import set_record_status

__all__ = ["DEFAULT_SCALE", "RISK_FIELDS", "RiskRepository"]

#: The two axes an operator may score, and the same bounds the database checks on the column: an
#: out-of-range number is a transcription error or a different scale, and either way it must not enter
#: the register.
AXIS_MIN = 1
AXIS_MAX = 5
#: The grid stated numbers are assumed to belong to when the source did not name one.  It is a label on
#: the row, not a rule about arithmetic: 1..5 on both axes is what the check constraint allows.
DEFAULT_SCALE = "MATRIX_5X5"

#: The four band words the platform recognises, so a register sorted by band can be trusted to have been
#: written with those words.  Nothing here maps a number onto a band.
SEVERITY_BANDS: tuple[str, ...] = tuple(str(level.value) for level in SeverityLevel)

#: Worst first, so a register reads top-down.  This is an ordering of words, not a scoring rule: it says
#: where "CRITICAL" sits relative to "LOW", never which band a number belongs to.
BAND_ORDER: tuple[str, ...] = (
    str(SeverityLevel.CRITICAL.value),
    str(SeverityLevel.HIGH.value),
    str(SeverityLevel.MEDIUM.value),
    str(SeverityLevel.LOW.value),
)

#: The columns a caller may set on a risk.  The assessment columns are in here on purpose: this layer
#: stores what a source said, and a scoring engine that computes them later is a separate module.
#: ``revision`` is absent - it belongs to the chain, and a caller who could set it could make two rows
#: claim the same number.
RISK_FIELDS: frozenset[str] = frozenset(
    {
        "code",
        "title",
        "category",
        "description",
        "revision_label",
        "project_id",
        "field_id",
        "well_id",
        "section_id",
        "depth_from_value",
        "depth_from_unit",
        "depth_to_value",
        "depth_to_unit",
        "probability",
        "impact",
        "severity",
        "severity_band",
        "scale",
        "causes",
        "consequences",
        "mitigation",
        "contingency",
        "owner",
        "source_note",
        "provenance",
        "attributes",
    }
)


def _axis(value: object, label: str) -> int | None:
    """One axis of an assessment, as a whole number inside the range the column allows.

    ``None`` in, ``None`` out: "nobody scored this" is a fact the register keeps, and coercing it to 1
    would turn an empty cell into a claim that the risk is negligible.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = int(text)
    except ValueError as error:
        raise ValidationError(f"{label} must be a whole number", value=value) from error
    if not AXIS_MIN <= number <= AXIS_MAX:
        raise ValidationError(
            f"{label} must be between {AXIS_MIN} and {AXIS_MAX}",
            value=number,
            scale=DEFAULT_SCALE,
        )
    return number


def _band(value: object) -> str | None:
    """The band word as the platform spells it, or ``None`` when the row has no band.

    Aliases are resolved ("severe" to HIGH) because a source says "severe" and a register has four
    columns; a word that means nothing is refused rather than stored, because a row banded "quite bad"
    cannot be counted beside one banded "HIGH" and the reader would never be told which was which.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    matched = match_severity(text)
    if matched is None:
        raise ValidationError(
            f"cannot read a severity band out of {text!r}",
            known=sorted(SEVERITY_BANDS),
            hint="the band is stored as the source stated it; only the four known words are accepted",
        )
    return str(matched.value)


def _severity_number(value: object) -> int | None:
    """The severity as the source stated it - stored, never recomputed from the axes."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as error:
        raise ValidationError("severity must be a whole number", value=value) from error


def _string_list(value: object) -> list[str]:
    """The list-shaped columns, accepting a list of statements or one multi-line string."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.splitlines() if part.strip()]
    if isinstance(value, Sequence):
        return [str(item).strip() for item in value if str(item or "").strip()]
    raise ValidationError("expected a list of statements", value=repr(value)[:120])


def _prose(value: object, *, extra: Sequence[str] = ()) -> str:
    """Prose in, prose out - and a list of bullets is refused rather than joined into a blob.

    ``mitigation`` and ``contingency`` are text columns (the plan, in the words of whoever owns the
    risk), while ``causes`` and ``consequences`` are JSON lists the register can count.  Taking a list
    for the prose columns and joining it silently would store something that reads like a sentence and
    queries like nothing, so the caller is told which column they meant.  ``extra`` is where a second
    batch of measures can be handed over deliberately, one per line.
    """
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValidationError(
            "mitigation and contingency are prose; a list of measures belongs in causes/consequences",
            value=repr(value)[:120],
        )
    body = value.strip()
    more = [str(item).strip() for item in extra or () if str(item or "").strip()]
    if more:
        body = "\n".join([part for part in [body, *more] if part])
    return body


def _token(value: object) -> str:
    return snake_token(value)


class RiskRepository:
    """The risk register: assessments as stated, the evidence behind them, and who carries them."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- writing --------------------------------------------------------------
    def create_risk(
        self,
        *,
        title: str,
        code: str = "",
        category: str = "",
        description: str = "",
        probability: object = None,
        impact: object = None,
        severity: object = None,
        severity_band: object = None,
        scale: str = "",
        owner: str = "",
        causes: Sequence[str] | str = (),
        consequences: Sequence[str] | str = (),
        mitigation: str = "",
        contingency: str = "",
        response: Sequence[str] = (),
        status: RiskLifecycle | str | None = None,
        created_by: str = "system",
        origin: str = KnowledgeOrigin.MANUAL.value,
        provenance: Sequence[Mapping[str, Any]] | None = None,
        attributes: Mapping[str, Any] | None = None,
        **scope: Any,
    ) -> RiskRecord:
        """Add a risk to the register, keeping the assessment exactly as it was stated.

        A source that gave only ``severity`` gets a row with only ``severity``; one that gave the two
        axes and no product gets those and a null severity.  Both are honest rows, and neither is
        completed by this layer: a number the platform derived would sit in the same column as the
        operator's own and no later reader could tell them apart.
        """
        if not str(title or "").strip():
            raise ValidationError("a risk needs a title")
        self._check_scope(
            **{
                key: value
                for key, value in scope.items()
                if key in ("well_id", "field_id", "project_id", "section_id")
            }
        )
        row = RiskRecord(
            id=new_id("risk"),
            code=str(code or "").strip() or None,
            title=str(title).strip()[:400],
            category=_token(category),
            description=str(description or ""),
            revision=1,
            status=str(
                RISK_LIFECYCLE.parse(status) if status is not None else RISK_LIFECYCLE.initial
            ),
            project_id=str(scope.get("project_id") or "") or None,
            field_id=str(scope.get("field_id") or "") or None,
            well_id=str(scope.get("well_id") or "") or None,
            section_id=str(scope.get("section_id") or "") or None,
            depth_from_value=scope.get("depth_from_value"),
            depth_from_unit=str(scope.get("depth_from_unit") or "") or None,
            depth_to_value=scope.get("depth_to_value"),
            depth_to_unit=str(scope.get("depth_to_unit") or "") or None,
            probability=_axis(probability, "probability"),
            impact=_axis(impact, "impact"),
            severity=_severity_number(severity),
            severity_band=_band(severity_band),
            scale=str(scale or "").strip() or DEFAULT_SCALE,
            causes=_string_list(causes),
            consequences=_string_list(consequences),
            mitigation=_prose(mitigation, extra=response),
            contingency=_prose(contingency),
            owner=str(owner or "") or None,
            source_note=str(scope.get("source_note") or "") or None,
            provenance=[dict(item) for item in provenance or ()],
            origin=str(getattr(origin, "value", origin)),
            created_by=str(created_by or "") or "system",
            attributes=dict(attributes or {}),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def assess_risk(
        self,
        risk_id: str,
        *,
        probability: object = None,
        impact: object = None,
        severity: object = None,
        severity_band: object = None,
        by: str = "",
        note: str = "",
    ) -> RiskRecord:
        """Record a new assessment on the same row: an assessment is not a revision of the document.

        The row keeps ``revision`` alone here.  Changing what we think about a risk is not the same act
        as rewriting what the risk is - and a register that forked a new row every time somebody moved a
        probability would lose the history it is supposed to summarise.  Who decided, their note and the
        numbers they gave go into ``attributes["assessments"]``, so the sequence of judgements stays
        readable and each one remains attributable to a person rather than to the tool that stored it.
        """
        row = self.get_risk(risk_id)
        if not str(by or "").strip():
            raise ValidationError("an assessment needs an author", hint="pass by=<who assessed it>")
        if probability is not None:
            row.probability = _axis(probability, "probability")
        if impact is not None:
            row.impact = _axis(impact, "impact")
        if severity is not None:
            row.severity = _severity_number(severity)
        if severity_band is not None:
            row.severity_band = _band(severity_band)
        history = list((row.attributes or {}).get("assessments") or [])
        history.append(
            {
                "by": by,
                "note": str(note or ""),
                "probability": row.probability,
                "impact": row.impact,
                "severity": row.severity,
                "severity_band": row.severity_band,
                "scale": row.scale,
            }
        )
        attributes = dict(row.attributes or {})
        attributes["assessments"] = history
        row.attributes = attributes
        self.session.flush()
        return row

    def update_risk(self, risk_id: str, **values: Any) -> tuple[RiskRecord, dict[str, Any]]:
        """Change what the risk *says*, and report what moved.

        The assessment columns are updatable here like any other, because they are data the source
        stated; :meth:`assess_risk` is the better route for a re-scoring, since it also records who did
        it.  Only ``revision`` is refused, and it is refused because it would corrupt the chain.
        """
        row = self.get_risk(risk_id)
        if "revision" in values:
            raise ValidationError(
                "revision belongs to the chain, not to an update",
                hint="a new revision of a risk is a new row",
            )
        unknown = sorted(set(values) - RISK_FIELDS)
        if unknown:
            raise ValidationError(
                f"risk has no field named {', '.join(unknown)}",
                allowed=sorted(RISK_FIELDS),
            )
        applied: dict[str, Any] = {}
        for field, value in values.items():
            payload: Any = value
            if field in {"causes", "consequences"}:
                payload = _string_list(value)
            elif field in {"mitigation", "contingency"}:
                payload = _prose(value)
            elif field in {"probability", "impact"}:
                payload = _axis(value, field)
            elif field == "severity":
                payload = _severity_number(value)
            elif field == "severity_band":
                payload = _band(value)
            elif field == "category":
                payload = _token(value)
            elif field == "title":
                payload = str(value or "").strip()[:400]
            elif field == "provenance":
                payload = [dict(item) for item in value or ()]
            elif field == "attributes":
                payload = dict(value or {})
            setattr(row, field, payload)
            applied[field] = payload
        self.session.flush()
        return row, applied

    def set_risk_status(
        self,
        risk_id: str,
        new_status: RiskLifecycle | str,
        *,
        by: str = "",
        reason: str = "",
    ) -> RiskRecord:
        """Open, mitigate, close or supersede a risk - through the lifecycle, with a reason.

        Closing a risk is a judgement about the remaining exposure, not about whether the mitigation text
        exists, so the reason is required: "the well was drilled, the risk expired" is a legitimate
        closure and an empty string is not.
        """
        row = self.get_risk(risk_id)
        target = str(RISK_LIFECYCLE.parse(new_status))
        if (
            target in {str(RiskLifecycle.CLOSED), str(RiskLifecycle.MITIGATED)}
            and not str(reason or "").strip()
        ):
            raise ValidationError(
                "closing or mitigating a risk needs a reason",
                hint="pass reason=<what changed>",
            )
        set_record_status(
            self.session, row, new_status, by=by, reason=reason, lifecycle=RISK_LIFECYCLE
        )
        return row

    # -- reading --------------------------------------------------------------
    def get_risk(self, risk_id: str) -> RiskRecord:
        row = self.session.get(RiskRecord, str(risk_id))
        if row is None:
            raise ValidationError(f"no risk {risk_id!r}")
        return row

    def list_risks(
        self,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
        section_id: str = "",
        category: str = "",
        status: str = "",
        band: str = "",
        min_severity: int = 0,
        unscored_only: bool = False,
        include_closed: bool = True,
        limit: int = 500,
    ) -> list[RiskRecord]:
        statement = select(RiskRecord)
        if well_id or section_id:
            statement = statement.where(
                RiskRecord.well_id == well_id if well_id else RiskRecord.section_id == section_id
            )
        for label, value in (("field_id", field_id), ("project_id", project_id)):
            if value:
                scoped_wells = select(Well.id).where(getattr(Well, label) == value)
                statement = statement.where(
                    or_(
                        getattr(RiskRecord, label) == value,
                        RiskRecord.well_id.in_(scoped_wells),
                    )
                )
        if category:
            statement = statement.where(RiskRecord.category == _token(category))
        if status:
            statement = statement.where(RiskRecord.status == str(RISK_LIFECYCLE.parse(status)))
        if band:
            statement = statement.where(RiskRecord.severity_band == _band(band))
        if min_severity:
            statement = statement.where(RiskRecord.severity >= int(min_severity))
        if unscored_only:
            statement = statement.where(RiskRecord.severity.is_(None))
        if not include_closed:
            statement = statement.where(
                RiskRecord.status.in_([str(RiskLifecycle.OPEN), str(RiskLifecycle.MITIGATED)])
            )
        statement = statement.order_by(
            RiskRecord.severity.desc().nulls_last(), RiskRecord.title, RiskRecord.id
        )
        bounded = _bounded(limit)
        if bounded is not None:
            statement = statement.limit(bounded)
        return list(self.session.execute(statement).scalars())

    def register(
        self, *, field_id: str = "", well_id: str = "", project_id: str = ""
    ) -> dict[str, Any]:
        """The register as a person reads it: what is open, how bad, and what is unscored.

        The counts are deliberately not one number.  "3 critical, 2 unscored" is the sentence a drilling
        superintendent wants, and folding the unscored into a low band would turn a gap in the work into
        a claim that the well is safe.  The bands counted here are the words the rows carry, not a
        ranking this module inferred from them.
        """
        rows = self.list_risks(
            field_id=field_id,
            well_id=well_id,
            project_id=project_id,
            include_closed=False,
            limit=0,
        )
        bands: dict[str, int] = {}
        categories: dict[str, int] = {}
        unscored = 0
        owned = 0
        for row in rows:
            key = str(row.severity_band or "")
            if key:
                bands[key] = bands.get(key, 0) + 1
            else:
                unscored += 1
            category = str(row.category or "uncategorised")
            categories[category] = categories.get(category, 0) + 1
            if str(row.owner or "").strip():
                owned += 1
        return {
            "open_count": len(rows),
            "by_band": dict(sorted(bands.items(), key=lambda item: _band_rank(item[0]))),
            "by_category": dict(sorted(categories.items(), key=lambda item: (-item[1], item[0]))),
            "unscored": unscored,
            "with_owner": owned,
            "highest": [
                {
                    "id": row.id,
                    "code": row.code,
                    "title": row.title,
                    "probability": row.probability,
                    "impact": row.impact,
                    "severity": row.severity,
                    "severity_band": row.severity_band,
                    "scale": row.scale,
                    "status": row.status,
                    "owner": row.owner,
                    "well_id": row.well_id,
                    "field_id": row.field_id,
                }
                for row in rows[:5]
            ],
        }

    # -- evidence and controls ------------------------------------------------
    def cite_evidence(
        self,
        risk_id: str,
        *,
        knowledge_item_ids: Sequence[str] = (),
        document_version_ids: Sequence[str] = (),
        npt_ids: Sequence[str] = (),
        problem_ids: Sequence[str] = (),
        note: str = "",
    ) -> int:
        """Point the risk at the records that make it worth carrying.

        A risk with no evidence is a guess, and the register says so - which is only useful because the
        edge is real: :func:`~drilling_intelligence.database.integrity.check_knowledge_relations` reports
        it as a dangling reference the moment the evidence row goes away.
        """
        row = self.get_risk(risk_id)
        targets: list[tuple[str, str]] = []
        for item_id in knowledge_item_ids:
            targets.append(("knowledge_item", str(item_id)))
        for version_id in document_version_ids:
            targets.append(("document_version", str(version_id)))
        for npt_id in npt_ids:
            if self.session.get(NptRecord, str(npt_id)) is None:
                raise ValidationError(f"no npt record {npt_id!r}")
            targets.append(("npt_record", str(npt_id)))
        for problem_id in problem_ids:
            if self.session.get(ProblemOccurrence, str(problem_id)) is None:
                raise ValidationError(f"no problem {problem_id!r}")
            targets.append(("problem_occurrence", str(problem_id)))
        for target_type, target_id in targets:
            create_knowledge_relation(
                self.session,
                source_type="risk",
                source_id=row.id,
                relation=KnowledgeRelationType.RISK_CITES_EVIDENCE.value,
                target_type=target_type,
                target_id=target_id,
                note=str(note or "") or "risk evidence",
            )
        return len(targets)

    def derive_from_problem(self, risk_id: str, problem_id: str, *, note: str = "") -> None:
        """Say that this risk came out of something that actually happened.

        The direction matters: the problem is the evidence, the risk is the generalisation.  A risk with
        such an edge can be re-examined when the underlying record is corrected, and one without can only
        be re-read by hand.
        """
        row = self.get_risk(risk_id)
        if self.session.get(ProblemOccurrence, str(problem_id)) is None:
            raise ValidationError(f"no problem {problem_id!r}")
        create_knowledge_relation(
            self.session,
            source_type="risk",
            source_id=row.id,
            relation=KnowledgeRelationType.RISK_DERIVED_FROM_PROBLEM.value,
            target_type="problem_occurrence",
            target_id=str(problem_id),
            note=str(note or "") or "risk raised from a recorded problem",
        )

    def link_procedure(self, risk_id: str, procedure_id: str, *, note: str = "") -> None:
        """Attach the control that mitigates the risk.

        A mitigation written as a sentence in a risk row is an intention; it becomes a control when a
        procedure - versioned, approved, owned - is pointed at.  This method does not mint a best
        practice out of that link either: promoting something to "how this field works" is an editorial
        act with a reviewer attached, and it lives in
        :meth:`~drilling_intelligence.lessons.repository.LessonRepository.promote_to_best_practice`.
        """
        row = self.get_risk(risk_id)
        procedure = self.session.get(ProcedureRecord, str(procedure_id))
        if procedure is None:
            raise ValidationError(f"no procedure {procedure_id!r}")
        create_knowledge_relation(
            self.session,
            source_type="risk",
            source_id=row.id,
            relation=KnowledgeRelationType.RISK_MITIGATED_BY_PROCEDURE.value,
            target_type="procedure",
            target_id=procedure.id,
            note=str(note or "") or "mitigating procedure",
        )

    def affects_activity(
        self, risk_id: str, *, operation_id: str = "", npt_id: str = "", note: str = ""
    ) -> None:
        """Record what the risk was realised as, once it has happened."""
        row = self.get_risk(risk_id)
        if npt_id:
            if self.session.get(NptRecord, str(npt_id)) is None:
                raise ValidationError(f"no npt record {npt_id!r}")
            target_type, target_id = "npt_record", str(npt_id)
        elif operation_id:
            if self.session.get(WellOperation, str(operation_id)) is None:
                raise ValidationError(f"no operation {operation_id!r}")
            target_type, target_id = "well_operation", str(operation_id)
        else:
            raise ValidationError("a realised risk needs the operation or the NPT row it became")
        create_knowledge_relation(
            self.session,
            source_type="risk",
            source_id=row.id,
            relation=KnowledgeRelationType.RISK_AFFECTS_ACTIVITY.value,
            target_type=target_type,
            target_id=target_id,
            note=str(note or "") or "risk realised",
        )

    # -- internals ------------------------------------------------------------
    def _check_scope(self, **scope: str) -> None:
        well_id = str(scope.get("well_id") or "")
        field_id = str(scope.get("field_id") or "")
        project_id = str(scope.get("project_id") or "")
        section_id = str(scope.get("section_id") or "")
        if section_id:
            if self.session.get(WellSection, section_id) is None:
                raise ValidationError(f"no section {section_id!r}")
            if not well_id:
                raise ValidationError("a section-scoped risk needs well_id too")
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


def _bounded(limit: int) -> int | None:
    """A ``LIMIT`` value, with ``0`` meaning "do not limit".

    A register is paginated for a screen and unpaginated for a summary, from the same query.  Reading
    ``limit=0`` as ``LIMIT 0`` would report an empty field, which is a wrong answer to a question that
    has a right one.
    """
    return None if int(limit) <= 0 else int(limit)


def _band_rank(band: object) -> int:
    try:
        return BAND_ORDER.index(str(band))
    except ValueError:
        return len(BAND_ORDER)
