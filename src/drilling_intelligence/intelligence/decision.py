"""The decision read-model: one deterministic, evidence-aware pack per scope (V7.5).

``DecisionPack`` answers a single question - *what does the authoritative repository currently
know about this well, field or project, what differs from plan, what costs are stated, what risks
are carried, what has been learned, what patterns were observed and what recommendations are
pending* - while keeping every limitation of the underlying data visible.

What it is:

*   a **read-only, re-runnable, plain-value projection** over the existing repositories
    (:class:`~drilling_intelligence.intelligence.field.FieldIntelligence`,
    :class:`~drilling_intelligence.engineering.repository.EngineeringRepository`,
    :class:`~drilling_intelligence.engineering.costs.CostRepository`, and grouped SQL over the
    risk/learning/pattern/calculation tables).  No ORM object escapes into the pack and nothing
    is persisted: the pack carries a content identity computed from its own payload, so two
    runs over the same state produce the same identity.
*   a **scope-first structure**: the pack is built for exactly one of well, field or project,
    every section repeats the scope it was computed under, and field/project packs separate
    rows carried at that level from rows carried by their wells.
*   a **substrate for a future AI/RAG layer**: every section labels its claim kind
    (``FACT`` / ``DERIVED`` / ``NOT_CHECKABLE``) and every decision-relevant number carries an
    evidence reference, so a later consumer can distinguish what the repository stated from
    what it computed - without this layer ever adding interpretation.

What it is not:

*   no risk scoring, no probability x impact, no ranking, no prediction, no root cause, no cost
    forecast, no currency conversion, no optimisation.  Where the source states a number the
    pack reports it; where two numbers cannot be compared (mismatched units) the comparison is
    refused and labelled ``NOT_CHECKABLE``; where a value is missing it stays missing rather
    than becoming zero.  The arithmetic that *is* performed - plan/actual variance, per-currency
    cost sums, hours sums - is the arithmetic the existing contracts already define, folded from
    their outputs rather than reimplemented.

Evidence references use two honest forms: ``structured:<record-type>:<row-id>`` identities for
domains that are searchable and therefore retrievable (NPT, problems, well control, HSE,
lessons, recommendations), and bounded row-id samples or re-executable method references for
domains that have no search projection (cost, risk, patterns, calculations, plan/actual).  A
reference that pretended to be searchable would itself be a dangling evidence link.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from sqlalchemy import and_, case, func, or_, select

from ..core.enums import (
    LessonLifecycle,
    ProcedureLifecycle,
    RecommendationLifecycle,
    RiskLifecycle,
)
from ..core.errors import ValidationError
from ..core.hashing import sha256_obj
from ..database.models import (
    BestPractice,
    Calculation,
    CalculationInput,
    Field,
    FieldPattern,
    KnowledgeRelation,
    LessonLearned,
    Project,
    Recommendation,
    RiskRecord,
    Well,
    WellSection,
)
from ..engineering.costs import CostRepository
from ..engineering.repository import (
    DEPENDENCY_CURRENT,
    DEPENDENCY_STALE,
    DEPENDENCY_UNRESOLVED,
    EngineeringRepository,
)
from .field import FieldIntelligence

#: Claim kinds the pack uses.  They are deliberately few and deliberately excludes
#: ``INTERPRETATION`` and ``HYPOTHESIS``: this layer never emits either.  ``NOT_CHECKABLE`` is
#: the honest label for arithmetic the source does not license (mismatched units).
CLAIM_FACT = "FACT"
CLAIM_DERIVED = "DERIVED"
CLAIM_NOT_CHECKABLE = "NOT_CHECKABLE"

#: Freshness states, reusing the vocabulary the mission's freshness contract asks for.
FRESH_CURRENT = "CURRENT"
FRESH_STALE = "STALE"
FRESH_UNRESOLVED = "UNRESOLVED"
FRESH_NOT_APPLICABLE = "NOT_APPLICABLE"
FRESH_NOT_AVAILABLE = "NOT_AVAILABLE"

#: The cap on distinct calculation input subjects a single pack resolves through
#: ``calculation_impact``.  The cap is what keeps the pack's query budget independent of row
#: count: each subject costs a bounded number of queries, the number of subjects is bounded by
#: this constant, and a workspace that exceeds it gets an explicit truncation limitation rather
#: than a silently partial answer.
CALCULATION_SUBJECT_CAP = 32

#: Pack schema version, bumped when the JSON shape changes incompatibly.
DECISION_PACK_SCHEMA = "decision-pack/1"


def calculation_chain_case() -> Any:
    """The current/history expression for stored calculations - the chain decides, never status.

    Shared by the decision pack and the comparison pack so "current" can never mean two things
    in two read models: a child points back at the row it superseded, so anything *referenced* by
    a ``supersedes_id`` is history and everything else is current (the rule
    ``calculations_for(current_only=True)`` documents).
    """
    superseded = select(Calculation.supersedes_id).where(Calculation.supersedes_id.is_not(None))
    return case((Calculation.id.in_(superseded), "history"), else_="current")


def resolve_dependency_states(
    session: Any,
    input_rows: Sequence[tuple[Any, Any]],
    *,
    scoped_calc_ids: set[str],
    evidence_limit: int,
    subject_cap: int = CALCULATION_SUBJECT_CAP,
) -> tuple[dict[str, int], list[dict[str, Any]], bool]:
    """Dependency state for in-scope calculation inputs, through ``calculation_impact`` itself.

    ``input_rows`` are ``(subject_key, calculation_id)`` pairs already scope-filtered.  Each
    distinct subject is resolved through the repository's own impact report (never a re-derived
    copy of its state machine), capped at ``subject_cap`` so the query budget stays independent
    of row count, and the entries are intersected with ``scoped_calc_ids`` so a subject shared
    with another scope cannot leak foreign calculations in.  Returns ``(counts, entries,
    truncated)``.
    """
    subject_keys = sorted({str(key) for key, _ in input_rows if key})
    truncated = len(subject_keys) > int(subject_cap)
    counts = {DEPENDENCY_CURRENT: 0, DEPENDENCY_STALE: 0, DEPENDENCY_UNRESOLVED: 0}
    entries: list[dict[str, Any]] = []
    if truncated:
        return counts, entries, True
    repository = EngineeringRepository(session)
    seen: set[tuple[str, str]] = set()
    for key in subject_keys[: int(subject_cap)]:
        report = repository.calculation_impact(key, current_only=True)
        for entry in report.get("entries", []) or []:
            if str(entry.get("calculation_id")) not in scoped_calc_ids:
                continue
            dedupe = (str(entry.get("calculation_id")), str(entry.get("input_name")))
            if dedupe in seen:
                continue
            seen.add(dedupe)
            state = str(entry.get("dependency"))
            counts[state] = counts.get(state, 0) + 1
            if len(entries) < int(evidence_limit):
                entries.append(
                    {
                        "calculation_id": entry.get("calculation_id"),
                        "method_id": entry.get("method_id"),
                        "method_version": entry.get("method_version"),
                        "status": entry.get("status"),
                        "input_name": entry.get("input_name"),
                        "dependency": state,
                    }
                )
    return counts, entries, truncated


_SCOPE_KEYS = ("well_id", "field_id", "project_id")


def _iso(value: Any) -> Any:
    """Deterministic ISO text for a date/datetime/str, or ``None``."""
    if value is None or value == "":
        return None
    text = str(value)
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value.isoformat()
    return text


def _scope_payload(**scope: str) -> dict[str, str | None]:
    """Every scope key, always, null where it does not apply: a machine reader branches on the
    key's presence in the *schema*, not on whether one pack happened to mention a field."""
    return {key: (str(scope.get(key) or "") or None) for key in _SCOPE_KEYS}


# --------------------------------------------------------------------------------------
# The plain-value contract
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionPackRequest:
    """The exact request that produced a pack - kept so a pack can be re-run and compared."""

    well_id: str = ""
    field_id: str = ""
    project_id: str = ""
    since: Any = None
    until: Any = None
    detail: int = 1
    evidence_limit: int = 10

    def payload(self) -> dict[str, Any]:
        return {
            "well_id": self.well_id or None,
            "field_id": self.field_id or None,
            "project_id": self.project_id or None,
            "since": _iso(self.since),
            "until": _iso(self.until),
            "detail": int(self.detail),
            "evidence_limit": int(self.evidence_limit),
        }


@dataclass(frozen=True)
class DecisionSubject:
    """Who the pack is about, resolved from the workspace before any section is built."""

    kind: str  # "well" | "field" | "project"
    id: str
    name: str
    well_count: int | None = None  # populated for field/project subjects
    field_id: str | None = None  # the field a well belongs to, when it has one
    project_id: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "name": self.name,
            "well_count": self.well_count,
            "field_id": self.field_id,
            "project_id": self.project_id,
        }


@dataclass(frozen=True)
class EvidenceRef:
    """A bounded pointer from a section's numbers back to the rows that produced them.

    ``kind`` is ``rows`` when ``sample`` holds real row ids (bounded by the pack's evidence
    limit), ``aggregate`` when the number comes from a windowed aggregate method and the
    reference is the exact, re-executable method + scope that produced it, and ``records``
    when only counts exist (grouped SQL) with no extra query spent on ids.
    """

    section: str
    domain: str
    kind: str
    identity: str  # "structured" when ids are retrieval-resolvable, else "record" / "method"
    count: int
    sample: tuple[str, ...] = ()
    truncated: bool = False
    method: str = ""  # re-executable reference for aggregate refs
    scope: Mapping[str, Any] = dataclass_field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "domain": self.domain,
            "kind": self.kind,
            "identity": self.identity,
            "count": int(self.count),
            "sample": list(self.sample),
            "truncated": bool(self.truncated),
            "method": self.method or None,
            "scope": dict(self.scope),
        }


@dataclass(frozen=True)
class ExecutionSection:
    """Plan versus actual, folded from ``EngineeringRepository.plan_actual_summary``."""

    scope: Mapping[str, Any]
    claim_kind: str
    sections: int
    rows: int
    sections_with_target: int
    sections_without_target: int
    sections_without_actual: int
    by_status: Mapping[str, int]
    by_matched_by: Mapping[str, int]
    by_metric: Mapping[str, Mapping[str, Any]]
    incomparable: tuple[Mapping[str, Any], ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "scope": dict(self.scope),
            "claim_kind": self.claim_kind,
            "sections": self.sections,
            "rows": self.rows,
            "sections_with_target": self.sections_with_target,
            "sections_without_target": self.sections_without_target,
            "sections_without_actual": self.sections_without_actual,
            "by_status": dict(self.by_status),
            "by_matched_by": dict(self.by_matched_by),
            "by_metric": {key: dict(value) for key, value in self.by_metric.items()},
            "incomparable": [dict(row) for row in self.incomparable],
        }


@dataclass(frozen=True)
class OperationsSection:
    """NPT, problems, well control and HSE - the V7.3A aggregates, under one scope."""

    scope: Mapping[str, Any]
    claim_kind: str
    npt: Mapping[str, Any]
    problems: Mapping[str, Any]
    well_control: Mapping[str, Any]
    hse: Mapping[str, Any]

    def payload(self) -> dict[str, Any]:
        return {
            "scope": dict(self.scope),
            "claim_kind": self.claim_kind,
            "npt": dict(self.npt),
            "problems": dict(self.problems),
            "well_control": dict(self.well_control),
            "hse": dict(self.hse),
        }


@dataclass(frozen=True)
class EconomicsSection:
    """Planned and actual money, per currency, never summed across currencies."""

    scope: Mapping[str, Any]
    claim_kind: str
    summary: Mapping[str, Any]
    wbs_rollup: tuple[Mapping[str, Any], ...] = ()
    cbs_rollup: tuple[Mapping[str, Any], ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "scope": dict(self.scope),
            "claim_kind": self.claim_kind,
            "summary": dict(self.summary),
            "wbs_rollup": [dict(row) for row in self.wbs_rollup],
            "cbs_rollup": [dict(row) for row in self.cbs_rollup],
        }


@dataclass(frozen=True)
class RiskSection:
    """Risk counts as the source assessed them: STATED versus UNASSESSED, never scored."""

    scope: Mapping[str, Any]
    claim_kind: str
    current: int
    superseded: int
    by_status: Mapping[str, int]
    by_scope_level: Mapping[str, int]
    stated: Mapping[str, int]  # severity/probability/impact each split STATED | UNASSESSED
    severity_bands: Mapping[str, int]
    with_evidence: int
    without_evidence: int
    relations: Mapping[str, int]  # explicit knowledge-relation edges touching these risks

    def payload(self) -> dict[str, Any]:
        return {
            "scope": dict(self.scope),
            "claim_kind": self.claim_kind,
            "current": self.current,
            "superseded": self.superseded,
            "by_status": dict(self.by_status),
            "by_scope_level": dict(self.by_scope_level),
            "stated": dict(self.stated),
            "severity_bands": dict(self.severity_bands),
            "with_evidence": self.with_evidence,
            "without_evidence": self.without_evidence,
            "relations": dict(self.relations),
        }


@dataclass(frozen=True)
class LearningSection:
    """Approved lessons versus adopted practices - two different things, kept different."""

    scope: Mapping[str, Any]
    claim_kind: str
    lessons: Mapping[str, Any]  # by_status, current, excluded_non_current, approved
    practices: Mapping[str, Any]  # by_status, current, excluded_non_current, adopted

    def payload(self) -> dict[str, Any]:
        return {
            "scope": dict(self.scope),
            "claim_kind": self.claim_kind,
            "lessons": dict(self.lessons),
            "practices": dict(self.practices),
        }


@dataclass(frozen=True)
class RecommendationSection:
    """Proposals with their decisions and their explicit source links - never facts."""

    scope: Mapping[str, Any]
    claim_kind: str
    total: int
    by_status: Mapping[str, int]
    links: Mapping[str, int]  # how many recommendations explicitly cite each source domain
    with_evidence: int
    without_evidence: int

    def payload(self) -> dict[str, Any]:
        return {
            "scope": dict(self.scope),
            "claim_kind": self.claim_kind,
            "total": self.total,
            "by_status": dict(self.by_status),
            "links": dict(self.links),
            "with_evidence": self.with_evidence,
            "without_evidence": self.without_evidence,
        }


@dataclass(frozen=True)
class PatternSection:
    """Observed grouped recurrence with its staleness - never a prediction."""

    scope: Mapping[str, Any]
    claim_kind: str
    total: int
    by_status: Mapping[str, Mapping[str, Any]]  # count, stale, occurrences, wells, npt_hours...
    stale: int
    with_evidence: int
    without_evidence: int

    def payload(self) -> dict[str, Any]:
        return {
            "scope": dict(self.scope),
            "claim_kind": self.claim_kind,
            "total": self.total,
            "by_status": {key: dict(value) for key, value in self.by_status.items()},
            "stale": self.stale,
            "with_evidence": self.with_evidence,
            "without_evidence": self.without_evidence,
        }


@dataclass(frozen=True)
class CalculationSection:
    """Stored calculations as observations: never recalculated, never triggered."""

    scope: Mapping[str, Any]
    claim_kind: str
    total: int
    current: int
    history: int
    by_status: Mapping[str, int]
    by_method: Mapping[str, int]
    inputs: int
    subjects_resolved: int
    subjects_truncated: bool
    dependency: Mapping[str, int]  # CURRENT / STALE / UNRESOLVED over resolved subjects
    dependency_entries: tuple[Mapping[str, Any], ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "scope": dict(self.scope),
            "claim_kind": self.claim_kind,
            "total": self.total,
            "current": self.current,
            "history": self.history,
            "by_status": dict(self.by_status),
            "by_method": dict(self.by_method),
            "inputs": self.inputs,
            "subjects_resolved": self.subjects_resolved,
            "subjects_truncated": self.subjects_truncated,
            "dependency": dict(self.dependency),
            "dependency_entries": [dict(entry) for entry in self.dependency_entries],
        }


@dataclass(frozen=True)
class DecisionPack:
    """The whole deterministic decision surface for one scope, as plain values."""

    schema: str
    request: DecisionPackRequest
    subject: DecisionSubject
    summary: Mapping[str, Any]
    execution: ExecutionSection
    operations: OperationsSection
    economics: EconomicsSection
    risk: RiskSection
    learning: LearningSection
    recommendations: RecommendationSection
    patterns: PatternSection
    calculations: CalculationSection
    evidence: tuple[EvidenceRef, ...]
    limitations: tuple[str, ...]
    freshness: Mapping[str, str]
    observations: tuple[str, ...]
    identity: str

    def to_dict(self) -> dict[str, Any]:
        """The stable machine-readable form.  Key order follows the dataclass fields."""
        return {
            "schema": self.schema,
            "request": self.request.payload(),
            "subject": self.subject.payload(),
            "summary": dict(self.summary),
            "execution": self.execution.payload(),
            "operations": self.operations.payload(),
            "economics": self.economics.payload(),
            "risk": self.risk.payload(),
            "learning": self.learning.payload(),
            "recommendations": self.recommendations.payload(),
            "patterns": self.patterns.payload(),
            "calculations": self.calculations.payload(),
            "evidence": [ref.payload() for ref in self.evidence],
            "limitations": list(self.limitations),
            "freshness": dict(self.freshness),
            "observations": list(self.observations),
            "identity": self.identity,
        }


# --------------------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------------------


class DecisionIntelligence(FieldIntelligence):
    """Builds :class:`DecisionPack` by composing the authoritative read surfaces.

    It *is-a* :class:`FieldIntelligence` - not a copy of one.  The scope resolver
    (``_wells``) and the operational aggregates (``npt`` / ``problems`` / ``well_control`` /
    ``hse``) are inherited so the pack's numbers are the same numbers ``fields summary``
    reports: a separate quick query would drift the first time one of the two grew a filter.
    The engineering, cost, risk, learning, pattern and calculation surfaces are called on
    their own repositories rather than re-derived here.
    """

    # -- scope ---------------------------------------------------------------------

    def _one_scope(self, well_id: str, field_id: str, project_id: str) -> tuple[str, str]:
        """Exactly one scope key, because two would have to be merged to be answered.

        ``well_id``, ``field_id`` and ``project_id`` mean different things - a well pack must
        not quietly widen into its field, and a field pack must not silently intersect with a
        project - so a caller who names two of them gets an explicit error instead of an
        arbitrary precedence rule.
        """
        named = {
            key: value
            for key, value in (
                ("well_id", well_id),
                ("field_id", field_id),
                ("project_id", project_id),
            )
            if value
        }
        if not named:
            raise ValidationError(
                "a decision pack needs a scope",
                hint="pass exactly one of well_id, field_id or project_id",
            )
        if len(named) > 1:
            raise ValidationError(
                "a decision pack takes exactly one scope",
                hint="ask for a well, a field or a project - one pack, one subject",
                given=sorted(named),
            )
        key = next(iter(named))
        return key, str(named[key])

    def _resolve_subject(self, scope_key: str, scope_value: str) -> DecisionSubject:
        if scope_key == "well_id":
            well = self.session.get(Well, scope_value)
            if well is None:
                raise ValidationError(f"no well {scope_value!r}", hint="pass a well id that exists")
            return DecisionSubject(
                kind="well",
                id=str(well.id),
                name=str(well.name or well.id),
                field_id=str(well.field_id or "") or None,
                project_id=str(well.project_id or "") or None,
            )
        if scope_key == "field_id":
            row = self.session.get(Field, scope_value)
            if row is None:
                raise ValidationError(
                    f"no field {scope_value!r}", hint="pass a field id that exists"
                )
            count = int(
                self.session.execute(
                    select(func.count(Well.id)).where(Well.field_id == scope_value)
                ).scalar_one()
            )
            return DecisionSubject(
                kind="field",
                id=str(row.id),
                name=str(row.name or row.id),
                well_count=count,
                project_id=str(row.project_id or "") or None,
            )

        row = self.session.get(Project, scope_value)
        if row is None:
            raise ValidationError(
                f"no project {scope_value!r}", hint="pass a project id that exists"
            )
        count = int(
            self.session.execute(
                select(func.count(Well.id)).where(Well.project_id == scope_value)
            ).scalar_one()
        )
        return DecisionSubject(
            kind="project",
            id=str(row.id),
            name=str(row.name or row.id),
            well_count=count,
        )

    # -- membership predicates for the grouped SQL -----------------------------------

    def _sections_of_wells(self, wells: Any) -> Any:
        return select(WellSection.id).where(WellSection.well_id.in_(wells))

    def _membership(
        self,
        model: Any,
        *,
        scope_key: str,
        scope_value: str,
        wells: Any,
        has_section: bool = True,
        has_field: bool = True,
    ) -> Any:
        """Which rows belong to a scope, never crossing into a wider one.

        A row belongs when it is carried by a well in scope (or a section of one, for models
        whose writer can file a section reference without the well), or - for field/project
        packs only - when it is carried at that very level with no well at all.  A
        field-carried row is never presented inside a well pack; a project-carried row is
        never presented inside a field pack; a row with no scope at all belongs to no pack
        (site-scoped HSE is the one deliberate exception, and it comes from the inherited
        ``hse()`` method, which already labels it site-scoped).
        """
        parts: list[Any] = [model.well_id.in_(wells)]
        if has_section:
            parts.append(
                and_(
                    model.well_id.is_(None),
                    model.section_id.in_(self._sections_of_wells(wells)),
                )
            )
        if scope_key == "field_id" and has_field:
            parts.append(
                and_(
                    model.well_id.is_(None),
                    *([model.section_id.is_(None)] if has_section else []),
                    model.field_id == scope_value,
                )
            )
        if scope_key == "project_id":
            project_parts = [
                and_(
                    model.well_id.is_(None),
                    *([model.section_id.is_(None)] if has_section else []),
                    model.project_id == scope_value,
                )
            ]
            if has_field:
                fields_of_project = select(Field.id).where(Field.project_id == scope_value)
                project_parts.append(
                    and_(
                        model.well_id.is_(None),
                        *([model.section_id.is_(None)] if has_section else []),
                        model.project_id.is_(None),
                        model.field_id.in_(fields_of_project),
                    )
                )
            parts.extend(project_parts)
        return or_(*parts)

    @staticmethod
    def _level_case(model: Any) -> Any:
        """The level a row is carried at: well / field / project / site."""
        return case(
            (model.well_id.isnot(None), "well"),
            (model.section_id.isnot(None), "well"),
            (model.field_id.isnot(None), "field"),
            (model.project_id.isnot(None), "project"),
            else_="site",
        )

    # -- sections -------------------------------------------------------------------

    def _execution(
        self, *, scope_key: str, scope_value: str, detail: int
    ) -> tuple[ExecutionSection, list[str]]:
        rows = EngineeringRepository(self.session).plan_actual_summary(**{scope_key: scope_value})
        sections = {str(row["section_id"]) for row in rows}
        by_status: dict[str, int] = {}
        by_matched: dict[str, int] = {}
        by_metric: dict[str, dict[str, Any]] = {}
        incomparable: list[dict[str, Any]] = []
        with_target: set[str] = set()
        without_actual: set[str] = set()
        for row in rows:
            status = str(row["status"])
            matched = str(row.get("matched_by") or "NO_MATCH")
            by_status[status] = by_status.get(status, 0) + 1
            by_matched[matched] = by_matched.get(matched, 0) + 1
            if row.get("target_id"):
                with_target.add(str(row["section_id"]))
            if row.get("actual") is None and status not in ("NO_TARGET",):
                without_actual.add(str(row["section_id"]))
            metric = by_metric.setdefault(
                str(row["metric"]),
                {
                    "rows": 0,
                    "planned": 0,
                    "actual": 0,
                    "variance": 0,
                    "on_plan": 0,
                    "variance_rows": 0,
                    "no_plan": 0,
                    "no_actual": 0,
                    "incomparable": 0,
                    "actual_unit_unstated": 0,
                    "unit": None,
                },
            )
            metric["rows"] += 1
            if row.get("planned") is not None:
                metric["planned"] += 1
            if row.get("actual") is not None:
                metric["actual"] += 1
            if row.get("variance") is not None:
                metric["variance"] += 1
            if status == "ON_PLAN":
                metric["on_plan"] += 1
            elif status == "VARIANCE":
                metric["variance_rows"] += 1
            elif status == "NO_PLAN":
                metric["no_plan"] += 1
            elif status == "NO_ACTUAL":
                metric["no_actual"] += 1
            elif status == "INCOMPARABLE_UNITS":
                metric["incomparable"] += 1
            state = str(row.get("variance_state") or "")
            if state == "ACTUAL_UNIT_UNSTATED":
                metric["actual_unit_unstated"] += 1
            if row.get("unit") and not metric["unit"]:
                metric["unit"] = row["unit"]
            if status == "INCOMPARABLE_UNITS":
                incomparable.append(
                    {
                        "section_id": row["section_id"],
                        "section": row["section"],
                        "metric": row["metric"],
                        "planned_unit": row.get("unit"),
                        "actual_unit": row.get("actual_unit"),
                        "matched_by": matched,
                    }
                )
        section_ids = sorted(sections)
        incomparable_cap = max(int(detail), 0) * 10
        return ExecutionSection(
            scope=_scope_payload(**{scope_key: scope_value}),
            claim_kind=CLAIM_DERIVED,
            sections=len(sections),
            rows=len(rows),
            sections_with_target=len(with_target),
            sections_without_target=len(sections) - len(with_target),
            sections_without_actual=len(without_actual),
            by_status=dict(sorted(by_status.items())),
            by_matched_by=dict(sorted(by_matched.items())),
            by_metric=dict(sorted(by_metric.items())),
            incomparable=tuple(incomparable[:incomparable_cap]),
        ), section_ids

    def _operations(
        self,
        *,
        scope_key: str,
        scope_value: str,
        since: Any,
        until: Any,
    ) -> OperationsSection:
        scope = {scope_key: scope_value}
        npt = self.npt(**scope, since=since, until=until)
        problems = self.problems(**scope, since=since, until=until)
        well_control = self.well_control(**scope, since=since, until=until)
        hse = self.hse(**scope, since=since, until=until)
        return OperationsSection(
            scope=_scope_payload(**scope),
            claim_kind=CLAIM_DERIVED,
            npt={
                "rows": npt.get("rows", 0),
                "total_hours": npt.get("total_hours"),
                "unknown_duration": npt.get("unknown_duration", 0),
                "undated": npt.get("undated", 0),
                "by_category": npt.get("by_category", {}),
            },
            problems={
                "occurrences": problems.get("occurrences", 0),
                "by_type": problems.get("by_type", {}),
            },
            well_control={
                "events": well_control.get("events", 0),
                "by_event_type": well_control.get("by_event_type", {}),
                "undated": well_control.get("undated", 0),
            },
            hse={
                "incidents": hse.get("incidents", 0),
                "well_scoped_incidents": hse.get("well_scoped_incidents", 0),
                "site_scoped_incidents": hse.get("site_scoped_incidents", 0),
                "by_incident_type": hse.get("by_incident_type", {}),
                "undated": hse.get("undated", 0),
            },
        )

    def _economics(self, *, scope_key: str, scope_value: str, detail: int) -> EconomicsSection:
        costs = CostRepository(self.session)
        scope = {scope_key: scope_value}
        summary = costs.summary(current_only=True, **scope)
        wbs: tuple[Mapping[str, Any], ...] = ()
        cbs: tuple[Mapping[str, Any], ...] = ()
        if detail >= 1:
            wbs = tuple(costs.rollup(by="wbs", **scope))
            cbs = tuple(costs.rollup(by="cbs", **scope))
        return EconomicsSection(
            scope=_scope_payload(**scope),
            claim_kind=CLAIM_DERIVED,
            summary=summary,
            wbs_rollup=wbs,
            cbs_rollup=cbs,
        )

    def _risk(
        self, *, scope_key: str, scope_value: str, wells: Any, evidence_limit: int
    ) -> tuple[RiskSection, list[str]]:
        membership = self._membership(
            RiskRecord, scope_key=scope_key, scope_value=scope_value, wells=wells
        )
        # One grouped pass over the dimensions that matter.  The GROUP BY cardinality is
        # bounded by the status x band x state x level vocabulary - not by row count - which
        # is what makes this safe where ``list_risks()`` (row materialisation, limit 500) is a
        # screen, not a summary.
        level_case = self._level_case(RiskRecord)
        sev_case = case((RiskRecord.severity.is_(None), "UNASSESSED"), else_="STATED")
        prob_case = case((RiskRecord.probability.is_(None), "UNASSESSED"), else_="STATED")
        imp_case = case((RiskRecord.impact.is_(None), "UNASSESSED"), else_="STATED")
        evidence_case = case(
            (func.coalesce(func.json_array_length(RiskRecord.provenance), 0) > 0, 1),
            else_=0,
        )
        rows = list(
            self.session.execute(
                select(
                    RiskRecord.status,
                    level_case,
                    sev_case,
                    prob_case,
                    imp_case,
                    RiskRecord.severity_band,
                    evidence_case,
                    func.count(RiskRecord.id),
                )
                .where(membership)
                .group_by(
                    RiskRecord.status,
                    level_case,
                    sev_case,
                    prob_case,
                    imp_case,
                    RiskRecord.severity_band,
                    evidence_case,
                )
            )
        )
        by_status: dict[str, int] = {}
        by_level: dict[str, int] = {}
        stated = {"severity": 0, "probability": 0, "impact": 0}
        unassessed = {"severity": 0, "probability": 0, "impact": 0}
        bands: dict[str, int] = {}
        with_evidence = 0
        without_evidence = 0
        superseded = 0
        current = 0
        for status, level, sev, prob, imp, band, has_ev, raw_count in rows:
            n = int(raw_count)
            by_status[str(status)] = by_status.get(str(status), 0) + n
            by_level[str(level)] = by_level.get(str(level), 0) + n
            for key, value in (("severity", sev), ("probability", prob), ("impact", imp)):
                bucket = stated if value == "STATED" else unassessed
                bucket[key] += n
            if band:
                bands[str(band)] = bands.get(str(band), 0) + n
            if int(has_ev):
                with_evidence += n
            else:
                without_evidence += n
            if str(status) == str(RiskLifecycle.SUPERSEDED):
                superseded += n
            else:
                current += n
        # Explicit edges only: knowledge-relation rows pointing at (or from) these risks.
        risk_ids = select(RiskRecord.id).where(membership)
        relation_rows = list(
            self.session.execute(
                select(KnowledgeRelation.relation, func.count(KnowledgeRelation.id))
                .where(
                    or_(
                        and_(
                            KnowledgeRelation.source_type == "risk",
                            KnowledgeRelation.source_id.in_(risk_ids),
                        ),
                        and_(
                            KnowledgeRelation.target_type == "risk",
                            KnowledgeRelation.target_id.in_(risk_ids),
                        ),
                    )
                )
                .group_by(KnowledgeRelation.relation)
            )
        )
        relations = {str(name): int(count) for name, count in relation_rows}
        sample_ids = [
            str(row)
            for row in self.session.execute(
                select(RiskRecord.id)
                .where(membership)
                .order_by(RiskRecord.id)
                .limit(int(evidence_limit))
            ).scalars()
        ]
        total_rows = sum(by_status.values())
        section = RiskSection(
            scope=_scope_payload(**{scope_key: scope_value}),
            claim_kind=CLAIM_FACT,
            current=current,
            superseded=superseded,
            by_status=dict(sorted(by_status.items())),
            by_scope_level=dict(sorted(by_level.items())),
            stated={
                "severity_stated": stated["severity"],
                "severity_unassessed": unassessed["severity"],
                "probability_stated": stated["probability"],
                "probability_unassessed": unassessed["probability"],
                "impact_stated": stated["impact"],
                "impact_unassessed": unassessed["impact"],
            },
            severity_bands=dict(sorted(bands.items())),
            with_evidence=with_evidence,
            without_evidence=without_evidence,
            relations=dict(sorted(relations.items())),
        )
        evidence = EvidenceRef(
            section="risk",
            domain="risk_record",
            kind="rows",
            identity="record",
            count=total_rows,
            sample=tuple(sample_ids),
            truncated=total_rows > len(sample_ids),
            scope=_scope_payload(**{scope_key: scope_value}),
        )
        return section, [evidence] if total_rows else []

    def _learning(
        self, *, scope_key: str, scope_value: str, wells: Any, evidence_limit: int
    ) -> tuple[LearningSection, RecommendationSection, list[str]]:
        lesson_membership = self._membership(
            LessonLearned, scope_key=scope_key, scope_value=scope_value, wells=wells
        )
        practice_membership = self._membership(
            BestPractice, scope_key=scope_key, scope_value=scope_value, wells=wells
        )
        rec_membership = self._membership(
            Recommendation, scope_key=scope_key, scope_value=scope_value, wells=wells
        )

        def _status_fold(model: Any, membership: Any) -> tuple[dict[str, int], int, int]:
            rows = list(
                self.session.execute(
                    select(model.status, model.is_current, func.count(model.id))
                    .where(membership)
                    .group_by(model.status, model.is_current)
                )
            )
            by_status: dict[str, int] = {}
            current = 0
            excluded = 0
            for status, is_current, raw_count in rows:
                n = int(raw_count)
                if bool(is_current):
                    by_status[str(status)] = by_status.get(str(status), 0) + n
                    current += n
                else:
                    excluded += n
            return dict(sorted(by_status.items())), current, excluded

        lesson_status, lesson_current, lesson_excluded = _status_fold(
            LessonLearned, lesson_membership
        )
        practice_status, practice_current, practice_excluded = _status_fold(
            BestPractice, practice_membership
        )

        # Recommendations: status, evidence presence and the explicit source links in one
        # grouped pass - the link columns ARE the chain, so no edge is ever inferred from
        # category, date or well.
        rec_rows = list(
            self.session.execute(
                select(
                    Recommendation.status,
                    func.count(Recommendation.id),
                    func.sum(
                        case(
                            (
                                func.coalesce(func.json_array_length(Recommendation.evidence), 0)
                                > 0,
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    func.sum(case((Recommendation.pattern_id.isnot(None), 1), else_=0)),
                    func.sum(case((Recommendation.lesson_id.isnot(None), 1), else_=0)),
                    func.sum(case((Recommendation.practice_id.isnot(None), 1), else_=0)),
                    func.sum(case((Recommendation.problem_id.isnot(None), 1), else_=0)),
                    func.sum(case((Recommendation.risk_id.isnot(None), 1), else_=0)),
                    func.sum(case((Recommendation.procedure_id.isnot(None), 1), else_=0)),
                    func.sum(case((Recommendation.program_id.isnot(None), 1), else_=0)),
                )
                .where(rec_membership)
                .group_by(Recommendation.status)
            )
        )
        rec_by_status: dict[str, int] = {}
        rec_with_evidence = 0
        rec_without_evidence = 0
        rec_links = {
            "pattern": 0,
            "lesson": 0,
            "practice": 0,
            "problem": 0,
            "risk": 0,
            "procedure": 0,
            "program": 0,
        }
        for (
            status,
            raw_count,
            has_evidence,
            pattern,
            lesson,
            practice,
            problem,
            risk,
            procedure,
            program,
        ) in rec_rows:
            n = int(raw_count)
            rec_by_status[str(status)] = rec_by_status.get(str(status), 0) + n
            rec_with_evidence += int(has_evidence or 0)
            rec_without_evidence += n - int(has_evidence or 0)
            for key, value in (
                ("pattern", pattern),
                ("lesson", lesson),
                ("practice", practice),
                ("problem", problem),
                ("risk", risk),
                ("procedure", procedure),
                ("program", program),
            ):
                rec_links[key] += int(value or 0)
        rec_total = sum(rec_by_status.values())

        # Bounded id samples for the three learning domains (identity form documented on each
        # EvidenceRef: lessons and recommendations are structured/searchable, practices are
        # not).
        def _sample(model: Any, membership: Any) -> list[str]:
            return [
                str(row)
                for row in self.session.execute(
                    select(model.id).where(membership).order_by(model.id).limit(int(evidence_limit))
                ).scalars()
            ]

        evidence: list[EvidenceRef] = []
        if lesson_current + lesson_excluded:
            ids = _sample(LessonLearned, lesson_membership)
            evidence.append(
                EvidenceRef(
                    section="learning",
                    domain="lesson_learned",
                    kind="rows",
                    identity="structured",
                    count=lesson_current + lesson_excluded,
                    sample=tuple(ids),
                    truncated=lesson_current + lesson_excluded > len(ids),
                    scope=_scope_payload(**{scope_key: scope_value}),
                )
            )
        if practice_current + practice_excluded:
            ids = _sample(BestPractice, practice_membership)
            evidence.append(
                EvidenceRef(
                    section="learning",
                    domain="best_practice",
                    kind="rows",
                    identity="record",
                    count=practice_current + practice_excluded,
                    sample=tuple(ids),
                    truncated=practice_current + practice_excluded > len(ids),
                    scope=_scope_payload(**{scope_key: scope_value}),
                )
            )
        if rec_total:
            ids = _sample(Recommendation, rec_membership)
            evidence.append(
                EvidenceRef(
                    section="recommendations",
                    domain="recommendation",
                    kind="rows",
                    identity="structured",
                    count=rec_total,
                    sample=tuple(ids),
                    truncated=rec_total > len(ids),
                    scope=_scope_payload(**{scope_key: scope_value}),
                )
            )

        # The lesson -> best-practice chain, only where the stored relation proves it.
        lesson_ids = select(LessonLearned.id).where(lesson_membership)
        practice_ids = select(BestPractice.id).where(practice_membership)
        chain_rows = list(
            self.session.execute(
                select(KnowledgeRelation.relation, func.count(KnowledgeRelation.id))
                .where(
                    KnowledgeRelation.relation == "LESSON_BEST_PRACTICE",
                    or_(
                        and_(
                            KnowledgeRelation.source_type == "lesson",
                            KnowledgeRelation.source_id.in_(lesson_ids),
                        ),
                        and_(
                            KnowledgeRelation.target_type == "lesson",
                            KnowledgeRelation.target_id.in_(lesson_ids),
                        ),
                        and_(
                            KnowledgeRelation.source_type == "best_practice",
                            KnowledgeRelation.source_id.in_(practice_ids),
                        ),
                        and_(
                            KnowledgeRelation.target_type == "best_practice",
                            KnowledgeRelation.target_id.in_(practice_ids),
                        ),
                    ),
                )
                .group_by(KnowledgeRelation.relation)
            )
        )
        learning = LearningSection(
            scope=_scope_payload(**{scope_key: scope_value}),
            claim_kind=CLAIM_FACT,
            lessons={
                "by_status": lesson_status,
                "current": lesson_current,
                "excluded_non_current": lesson_excluded,
                "approved": lesson_status.get(str(LessonLifecycle.APPROVED), 0),
                "relations": {str(name): int(count) for name, count in chain_rows},
            },
            practices={
                "by_status": practice_status,
                "current": practice_current,
                "excluded_non_current": practice_excluded,
                "adopted": practice_status.get(str(ProcedureLifecycle.APPROVED), 0),
            },
        )
        recommendations = RecommendationSection(
            scope=_scope_payload(**{scope_key: scope_value}),
            claim_kind=CLAIM_FACT,
            total=rec_total,
            by_status=dict(sorted(rec_by_status.items())),
            links=dict(rec_links),
            with_evidence=rec_with_evidence,
            without_evidence=rec_without_evidence,
        )
        return learning, recommendations, evidence

    def _patterns(
        self,
        *,
        scope_key: str,
        scope_value: str,
        subject: DecisionSubject,
        evidence_limit: int,
    ) -> tuple[PatternSection, list[str]]:
        # Patterns are filed at field or project level - the model carries no well column - so
        # a well pack reads its well's field's patterns and says so in the section scope.  The
        # level is never laundered into a well-level claim.
        pattern_scope: dict[str, str] = {}
        if scope_key == "well_id":
            if not subject.field_id:
                return (
                    PatternSection(
                        scope=_scope_payload(),
                        claim_kind=CLAIM_FACT,
                        total=0,
                        by_status={},
                        stale=0,
                        with_evidence=0,
                        without_evidence=0,
                    ),
                    [],
                )
            pattern_scope = {"field_id": subject.field_id}
        elif scope_key == "field_id":
            pattern_scope = {"field_id": scope_value}
        else:
            pattern_scope = {"project_id": scope_value}
        clauses: list[Any] = []
        if pattern_scope.get("field_id"):
            clauses.append(FieldPattern.field_id == pattern_scope["field_id"])
        if pattern_scope.get("project_id"):
            clauses.append(FieldPattern.project_id == pattern_scope["project_id"])
            fields_of_project = select(Field.id).where(
                Field.project_id == pattern_scope["project_id"]
            )
            clauses.append(FieldPattern.field_id.in_(fields_of_project))
        membership = or_(*clauses)
        stale_case = case((FieldPattern.stale_at.isnot(None), 1), else_=0)
        rows = list(
            self.session.execute(
                select(
                    FieldPattern.status,
                    stale_case.label("stale"),
                    func.count(FieldPattern.id),
                    func.coalesce(func.sum(FieldPattern.occurrence_count), 0),
                    func.coalesce(func.sum(FieldPattern.well_count), 0),
                    func.coalesce(func.sum(FieldPattern.total_npt_hours), 0.0),
                    func.sum(
                        case(
                            (
                                func.coalesce(func.json_array_length(FieldPattern.evidence), 0) > 0,
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    func.sum(case((FieldPattern.total_npt_hours.isnot(None), 1), else_=0)),
                )
                .where(membership)
                .group_by(FieldPattern.status, "stale")
            )
        )
        by_status: dict[str, dict[str, Any]] = {}
        stale = 0
        with_evidence = 0
        without_evidence = 0
        total = 0
        for (
            status,
            is_stale,
            raw_count,
            occurrences,
            wells,
            npt_hours,
            evidence_rows,
            npt_rows,
        ) in rows:
            n = int(raw_count)
            total += n
            stale += n if int(is_stale) else 0
            bucket = by_status.setdefault(
                str(status),
                {
                    "count": 0,
                    "stale": 0,
                    "occurrences": 0,
                    "wells": 0,
                    "npt_hours": 0.0,
                    "rows_with_npt_hours": 0,
                    "with_evidence": 0,
                    "without_evidence": 0,
                },
            )
            bucket["count"] += n
            bucket["stale"] += n if int(is_stale) else 0
            bucket["occurrences"] += int(occurrences or 0)
            bucket["wells"] += int(wells or 0)
            bucket["npt_hours"] = round(float(bucket["npt_hours"]) + float(npt_hours or 0.0), 4)
            bucket["rows_with_npt_hours"] += int(npt_rows or 0)
            bucket["with_evidence"] += int(evidence_rows or 0)
            bucket["without_evidence"] += n - int(evidence_rows or 0)
            with_evidence += int(evidence_rows or 0)
            without_evidence += n - int(evidence_rows or 0)
        evidence: list[EvidenceRef] = []
        if total:
            ids = [
                str(row)
                for row in self.session.execute(
                    select(FieldPattern.id)
                    .where(membership)
                    .order_by(FieldPattern.id)
                    .limit(int(evidence_limit))
                ).scalars()
            ]
            evidence.append(
                EvidenceRef(
                    section="patterns",
                    domain="field_pattern",
                    kind="rows",
                    identity="record",
                    count=total,
                    sample=tuple(ids),
                    truncated=total > len(ids),
                    scope=_scope_payload(**pattern_scope),
                )
            )
        return (
            PatternSection(
                scope=_scope_payload(**pattern_scope),
                claim_kind=CLAIM_FACT,
                total=total,
                by_status=dict(sorted(by_status.items())),
                stale=stale,
                with_evidence=with_evidence,
                without_evidence=without_evidence,
            ),
            evidence,
        )

    def _calculations(
        self, *, scope_key: str, scope_value: str, wells: Any, evidence_limit: int
    ) -> tuple[CalculationSection, list[str]]:
        # Calculation carries well/section/project but no field column: a field pack scopes it
        # through its wells, and a project pack also admits project-carried rows.
        clauses: list[Any] = [Calculation.well_id.in_(wells)]
        clauses.append(
            and_(
                Calculation.well_id.is_(None),
                Calculation.section_id.in_(self._sections_of_wells(wells)),
            )
        )
        if scope_key == "project_id":
            clauses.append(
                and_(
                    Calculation.well_id.is_(None),
                    Calculation.section_id.is_(None),
                    Calculation.project_id == scope_value,
                )
            )
        membership = or_(*clauses)
        # Current/history for calculations is decided by the revision chain, exactly as
        # ``calculations_for(current_only=True)`` defines it; the shared expression is what the
        # comparison pack reads too, so the two read models cannot disagree about "current".
        chain_case = calculation_chain_case()

        state_rows = list(
            self.session.execute(
                select(chain_case, Calculation.status, func.count(Calculation.id))
                .where(membership)
                .group_by(chain_case, Calculation.status)
            )
        )
        by_status: dict[str, int] = {}
        current = 0
        history = 0
        for chain_state, status, raw_count in state_rows:
            n = int(raw_count)
            by_status[str(status)] = by_status.get(str(status), 0) + n
            if str(chain_state) == "current":
                current += n
            else:
                history += n

        method_rows = list(
            self.session.execute(
                select(Calculation.method_id, func.count(Calculation.id))
                .where(membership)
                .group_by(Calculation.method_id)
            )
        )
        by_method = {str(name): int(count) for name, count in method_rows}

        # Inputs in scope in one join: gives the per-pack input total and the distinct subject
        # keys the dependency pass will resolve - one query, not one per calculation.
        input_rows = list(
            self.session.execute(
                select(CalculationInput.subject_key, CalculationInput.calculation_id)
                .join(
                    Calculation,
                    Calculation.id == CalculationInput.calculation_id,
                )
                .where(membership)
            )
        )
        inputs_total = len(input_rows)
        scoped_calc_ids = {str(calc_id) for _, calc_id in input_rows}
        # ``calculation_impact`` is resolved per subject *without* a scope filter and then
        # intersected with this pack's own in-scope calculation ids inside the shared fold - the
        # intersection is what keeps a project-level subject from leaking another field's
        # calculations into this pack.
        dependency, entries, truncated = resolve_dependency_states(
            self.session,
            input_rows,
            scoped_calc_ids=scoped_calc_ids,
            evidence_limit=evidence_limit,
        )
        subjects_resolved = 0 if truncated else len({str(key) for key, _ in input_rows if key})
        section = CalculationSection(
            scope=_scope_payload(**{scope_key: scope_value}),
            claim_kind=CLAIM_FACT,
            total=current + history,
            current=current,
            history=history,
            by_status=dict(sorted(by_status.items())),
            by_method=dict(sorted(by_method.items())),
            inputs=inputs_total,
            subjects_resolved=subjects_resolved,
            subjects_truncated=truncated,
            dependency=dependency,
            dependency_entries=tuple(entries),
        )
        evidence: list[EvidenceRef] = []
        if current + history:
            ids = sorted(scoped_calc_ids)[: int(evidence_limit)]
            evidence.append(
                EvidenceRef(
                    section="calculations",
                    domain="calculation",
                    kind="rows",
                    identity="record",
                    count=current + history,
                    sample=tuple(ids),
                    truncated=(current + history) > len(ids),
                    scope=_scope_payload(**{scope_key: scope_value}),
                )
            )
        return section, evidence

    def _cost_evidence(
        self, *, scope_key: str, scope_value: str, evidence_limit: int
    ) -> list[EvidenceRef]:
        from ..database.models import CostItem

        # ``_scope_statement`` is the very statement ``summary()`` materialises, reused rather
        # than mirrored: an evidence sample that disagreed with the total it backs would be worse
        # than no sample at all.
        base = CostRepository(self.session)._scope_statement(
            {scope_key: scope_value}, current_only=True
        )
        scope_filter = base.whereclause  # current-only + the scope predicates, AND-ed in
        total = int(
            self.session.execute(select(func.count(CostItem.id)).where(scope_filter)).scalar_one()
        )
        if not total:
            return []
        ids = [
            str(row)
            for row in self.session.execute(
                select(CostItem.id)
                .where(scope_filter)
                .order_by(CostItem.id)
                .limit(int(evidence_limit))
            ).scalars()
        ]
        return [
            EvidenceRef(
                section="economics",
                domain="cost_item",
                kind="rows",
                identity="record",
                count=total,
                sample=tuple(ids),
                truncated=total > len(ids),
                scope=_scope_payload(**{scope_key: scope_value}),
            )
        ]

    @staticmethod
    def _execution_evidence(
        section: ExecutionSection, section_ids: Sequence[str], evidence_limit: int
    ) -> list[EvidenceRef]:
        # The ids are the very sections the fold compared - no second query that could ever
        # disagree with the count it documents.
        if not section.sections:
            return []
        ids = list(section_ids)[: int(evidence_limit)]
        return [
            EvidenceRef(
                section="execution",
                domain="well_section",
                kind="rows",
                identity="record",
                count=section.sections,
                sample=tuple(ids),
                truncated=section.sections > len(ids),
                method="EngineeringRepository.plan_actual_summary",
                scope=dict(section.scope),
            )
        ]

    # -- fold: limitations, observations, freshness ---------------------------------

    @staticmethod
    def _limitations(
        *,
        execution: ExecutionSection,
        operations: OperationsSection,
        economics: EconomicsSection,
        risk: RiskSection,
        recommendations: RecommendationSection,
        patterns: PatternSection,
        calculations: CalculationSection,
        evidence: Sequence[EvidenceRef],
    ) -> tuple[str, ...]:
        found: list[str] = []
        status = execution.by_status
        if status.get("NO_PLAN", 0):
            found.append("missing_plan")
        if status.get("NO_ACTUAL", 0):
            found.append("missing_actual")
        if status.get("NO_TARGET", 0):
            found.append("missing_plan")
        matched = execution.by_matched_by
        if matched.get("SECTION_ID_AMBIGUOUS", 0) or matched.get("NAME_AMBIGUOUS", 0):
            found.append("ambiguous_plan_match")
        if matched.get("NAME", 0) or matched.get("NAME_AMBIGUOUS", 0):
            found.append("plan_matched_by_name")
        if status.get("INCOMPARABLE_UNITS", 0):
            found.append("incomparable_units")
        if any(metric.get("actual_unit_unstated", 0) for metric in execution.by_metric.values()):
            found.append("actual_unit_unstated")

        cost = economics.summary
        if cost.get("mixed_currency"):
            found.append("mixed_currency")
        if cost.get("mixed_currency_lines", 0):
            found.append("mixed_unit_line")
        if cost.get("unpriced", 0):
            found.append("unpriced_cost_lines")
        for entry in (cost.get("by_currency") or {}).values():
            if entry.get("actual_lines", 0) and not entry.get("planned_lines", 0):
                found.append("cost_plan_missing")
            if entry.get("planned_lines", 0) and not entry.get("actual_lines", 0):
                found.append("cost_actual_missing")

        if operations.npt.get("unknown_duration", 0):
            found.append("unknown_npt_duration")
        if (
            operations.npt.get("undated", 0)
            or operations.well_control.get("undated", 0)
            or operations.hse.get("undated", 0)
        ):
            found.append("undated_record")
        if operations.hse.get("site_scoped_incidents", 0):
            found.append("site_scoped_hse")

        if risk.stated.get("severity_unassessed", 0):
            found.append("unassessed_risk")
        if risk.without_evidence:
            found.append("no_evidence")

        if recommendations.without_evidence:
            found.append("no_evidence")

        if patterns.stale:
            found.append("stale_pattern")

        if calculations.dependency.get(DEPENDENCY_STALE, 0):
            found.append("stale_dependency")
        if calculations.dependency.get(DEPENDENCY_UNRESOLVED, 0):
            found.append("unresolved_dependency")
        if calculations.subjects_truncated:
            found.append("dependency_subjects_truncated")

        if any(ref.truncated for ref in evidence):
            found.append("evidence_truncated")

        return tuple(dict.fromkeys(found))  # deterministic order, no duplicates

    @staticmethod
    def _freshness(
        *,
        execution: ExecutionSection,
        operations: OperationsSection,
        economics: EconomicsSection,
        risk: RiskSection,
        learning: LearningSection,
        recommendations: RecommendationSection,
        patterns: PatternSection,
        calculations: CalculationSection,
    ) -> dict[str, str]:
        def _empty(*values: Any) -> bool:
            return not any(values)

        freshness: dict[str, str] = {}
        freshness["execution"] = FRESH_NOT_APPLICABLE if not execution.sections else FRESH_CURRENT
        freshness["operations"] = (
            FRESH_NOT_APPLICABLE
            if _empty(
                operations.npt.get("rows", 0),
                operations.problems.get("occurrences", 0),
                operations.well_control.get("events", 0),
                operations.hse.get("incidents", 0),
            )
            else FRESH_CURRENT
        )
        freshness["economics"] = (
            FRESH_NOT_APPLICABLE if not economics.summary.get("items", 0) else FRESH_CURRENT
        )
        freshness["risk"] = (
            FRESH_NOT_APPLICABLE if not (risk.current + risk.superseded) else FRESH_CURRENT
        )
        freshness["learning"] = (
            FRESH_NOT_APPLICABLE
            if _empty(
                learning.lessons.get("current", 0),
                learning.lessons.get("excluded_non_current", 0),
                learning.practices.get("current", 0),
                learning.practices.get("excluded_non_current", 0),
            )
            else FRESH_CURRENT
        )
        freshness["recommendations"] = (
            FRESH_NOT_APPLICABLE if not recommendations.total else FRESH_CURRENT
        )
        freshness["patterns"] = FRESH_NOT_APPLICABLE if not patterns.total else FRESH_CURRENT
        if not calculations.total:
            freshness["calculations"] = FRESH_NOT_APPLICABLE
        elif calculations.subjects_truncated:
            freshness["calculations"] = FRESH_NOT_AVAILABLE
        elif calculations.dependency.get(DEPENDENCY_UNRESOLVED, 0):
            freshness["calculations"] = FRESH_UNRESOLVED
        elif calculations.dependency.get(DEPENDENCY_STALE, 0):
            freshness["calculations"] = FRESH_STALE
        else:
            freshness["calculations"] = FRESH_CURRENT
        return dict(sorted(freshness.items()))

    @staticmethod
    def _observations(
        *,
        subject: DecisionSubject,
        execution: ExecutionSection,
        operations: OperationsSection,
        economics: EconomicsSection,
        risk: RiskSection,
        learning: LearningSection,
        recommendations: RecommendationSection,
        patterns: PatternSection,
    ) -> tuple[str, ...]:
        """Deterministic, count-derived sentences.  Each one is arithmetic over section
        values already in the pack - none of them interprets, ranks, predicts or judges."""
        out: list[str] = []
        name = subject.name or subject.id

        no_plan_rows = execution.by_status.get("NO_PLAN", 0)
        if no_plan_rows:
            out.append(f"{no_plan_rows} plan/actual comparisons have no matching plan.")
        if execution.by_status.get("NO_ACTUAL", 0):
            out.append(
                f"{execution.by_status['NO_ACTUAL']} plan/actual comparisons have a plan but no actual."
            )
        if execution.by_status.get("INCOMPARABLE_UNITS", 0):
            out.append(
                f"{execution.by_status['INCOMPARABLE_UNITS']} comparisons state units that differ "
                "between plan and actual; no variance is reported for them."
            )

        currencies = list((economics.summary.get("by_currency") or {}).keys())
        if len(currencies) > 1:
            joined = " and ".join(sorted(currencies))
            out.append(
                f"Two or more current cost currencies are present ({joined}); "
                "no combined monetary total is reported."
            )
        elif len(currencies) == 1:
            out.append(
                f"Costs are stated in {currencies[0]}; planned and actual remain separate totals."
            )

        unassessed = risk.stated.get("severity_unassessed", 0)
        if risk.current and unassessed:
            out.append(f"{unassessed} current risks have no stated severity.")
        elif risk.current and not unassessed:
            out.append(f"{risk.current} current risks carry a source-stated severity.")

        proposed = recommendations.by_status.get(str(RecommendationLifecycle.PROPOSED), 0)
        accepted = recommendations.by_status.get(str(RecommendationLifecycle.ACCEPTED), 0)
        if proposed:
            out.append(
                f"{proposed} recommendations are proposed"
                + (f" and {accepted} accepted" if accepted else " and none accepted")
                + "."
            )

        if patterns.stale:
            out.append(f"{patterns.stale} observed patterns are marked stale.")
        confirmed = patterns.by_status.get("CONFIRMED", {}).get("count", 0)
        if confirmed:
            out.append(f"{confirmed} patterns are confirmed observations.")

        wc = operations.well_control.get("events", 0)
        hse = operations.hse.get("incidents", 0)
        if wc or hse:
            out.append(f"{name} has {wc} well-control rows and {hse} HSE incidents in scope.")
        approved = learning.lessons.get("approved", 0)
        adopted = learning.practices.get("adopted", 0)
        if approved or adopted:
            out.append(
                f"{approved} approved lessons and {adopted} adopted practices are on record."
            )
        return tuple(out)

    # -- the pack --------------------------------------------------------------------

    def pack(
        self,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
        since: Any = None,
        until: Any = None,
        detail: int = 1,
        evidence_limit: int = 10,
    ) -> DecisionPack:
        """Build the deterministic decision pack for exactly one scope.

        The pack is re-runnable: same request, same repository state, same content identity.
        Nothing is persisted and no aggregate is cached, because a decision number computed
        yesterday is exactly the staleness this layer exists to make visible.
        """
        scope_key, scope_value = self._one_scope(well_id, field_id, project_id)
        subject = self._resolve_subject(scope_key, scope_value)
        request = DecisionPackRequest(
            well_id=well_id,
            field_id=field_id,
            project_id=project_id,
            since=since,
            until=until,
            detail=int(detail),
            evidence_limit=max(int(evidence_limit), 0),
        )
        wells = self._wells(
            well_id=scope_value if scope_key == "well_id" else "",
            field_id=scope_value if scope_key == "field_id" else "",
            project_id=scope_value if scope_key == "project_id" else "",
        )

        execution, execution_section_ids = self._execution(
            scope_key=scope_key, scope_value=scope_value, detail=request.detail
        )
        operations = self._operations(
            scope_key=scope_key, scope_value=scope_value, since=since, until=until
        )
        economics = self._economics(
            scope_key=scope_key, scope_value=scope_value, detail=request.detail
        )
        risk, risk_evidence = self._risk(
            scope_key=scope_key,
            scope_value=scope_value,
            wells=wells,
            evidence_limit=request.evidence_limit,
        )
        learning, recommendations, learning_evidence = self._learning(
            scope_key=scope_key,
            scope_value=scope_value,
            wells=wells,
            evidence_limit=request.evidence_limit,
        )
        patterns, pattern_evidence = self._patterns(
            scope_key=scope_key,
            scope_value=scope_value,
            subject=subject,
            evidence_limit=request.evidence_limit,
        )
        calculations, calculation_evidence = self._calculations(
            scope_key=scope_key,
            scope_value=scope_value,
            wells=wells,
            evidence_limit=request.evidence_limit,
        )
        cost_evidence = self._cost_evidence(
            scope_key=scope_key,
            scope_value=scope_value,
            evidence_limit=request.evidence_limit,
        )
        execution_evidence = self._execution_evidence(
            execution, execution_section_ids, request.evidence_limit
        )

        # Aggregate-backed domains: the exact re-executable reference rather than sampled ids.
        windowed_scope = _scope_payload(**{scope_key: scope_value})
        windowed_scope["since"] = _iso(since)
        windowed_scope["until"] = _iso(until)
        operations_evidence = [
            EvidenceRef(
                section="operations",
                domain=name,
                kind="aggregate",
                identity="method",
                count=int(count),
                method=method,
                scope=windowed_scope,
            )
            for name, method, count in (
                ("npt_record", "FieldIntelligence.npt", operations.npt.get("rows", 0)),
                (
                    "problem_occurrence",
                    "FieldIntelligence.problems",
                    operations.problems.get("occurrences", 0),
                ),
                (
                    "well_control_event",
                    "FieldIntelligence.well_control",
                    operations.well_control.get("events", 0),
                ),
                (
                    "hse_incident",
                    "FieldIntelligence.hse",
                    operations.hse.get("incidents", 0),
                ),
            )
            if count
        ]

        evidence = [
            *execution_evidence,
            *operations_evidence,
            *cost_evidence,
            *risk_evidence,
            *learning_evidence,
            *pattern_evidence,
            *calculation_evidence,
        ]
        limitations = self._limitations(
            execution=execution,
            operations=operations,
            economics=economics,
            risk=risk,
            recommendations=recommendations,
            patterns=patterns,
            calculations=calculations,
            evidence=evidence,
        )
        freshness = self._freshness(
            execution=execution,
            operations=operations,
            economics=economics,
            risk=risk,
            learning=learning,
            recommendations=recommendations,
            patterns=patterns,
            calculations=calculations,
        )
        observations = self._observations(
            subject=subject,
            execution=execution,
            operations=operations,
            economics=economics,
            risk=risk,
            learning=learning,
            recommendations=recommendations,
            patterns=patterns,
        )
        summary = {
            "sections": execution.sections,
            "on_plan": execution.by_status.get("ON_PLAN", 0),
            "variance": execution.by_status.get("VARIANCE", 0),
            "no_plan": execution.by_status.get("NO_PLAN", 0),
            "no_actual": execution.by_status.get("NO_ACTUAL", 0),
            "npt_hours": operations.npt.get("total_hours"),
            "problems": operations.problems.get("occurrences", 0),
            "well_control_events": operations.well_control.get("events", 0),
            "hse_incidents": operations.hse.get("incidents", 0),
            "cost_currencies": sorted((economics.summary.get("by_currency") or {}).keys()),
            "open_risks": risk.by_status.get(str(RiskLifecycle.OPEN), 0),
            "unassessed_risks": risk.stated.get("severity_unassessed", 0),
            "approved_lessons": learning.lessons.get("approved", 0),
            "adopted_practices": learning.practices.get("adopted", 0),
            "proposed_recommendations": recommendations.by_status.get(
                str(RecommendationLifecycle.PROPOSED), 0
            ),
            "confirmed_patterns": patterns.by_status.get("CONFIRMED", {}).get("count", 0),
            "stale_patterns": patterns.stale,
            "calculations": calculations.total,
            "limitations": len(limitations),
        }

        core = DecisionPack(
            schema=DECISION_PACK_SCHEMA,
            request=request,
            subject=subject,
            summary=summary,
            execution=execution,
            operations=operations,
            economics=economics,
            risk=risk,
            learning=learning,
            recommendations=recommendations,
            patterns=patterns,
            calculations=calculations,
            evidence=tuple(evidence),
            limitations=limitations,
            freshness=freshness,
            observations=observations,
            identity="",  # filled below from the payload itself
        )
        identity = sha256_obj(
            {
                "schema": core.schema,
                "request": core.request.payload(),
                "subject": core.subject.payload(),
                "summary": dict(core.summary),
                "execution": core.execution.payload(),
                "operations": core.operations.payload(),
                "economics": core.economics.payload(),
                "risk": core.risk.payload(),
                "learning": core.learning.payload(),
                "recommendations": core.recommendations.payload(),
                "patterns": core.patterns.payload(),
                "calculations": core.calculations.payload(),
                "limitations": list(core.limitations),
                "freshness": dict(core.freshness),
                "observations": list(core.observations),
            }
        )
        return DecisionPack(
            schema=core.schema,
            request=core.request,
            subject=core.subject,
            summary=core.summary,
            execution=core.execution,
            operations=core.operations,
            economics=core.economics,
            risk=core.risk,
            learning=core.learning,
            recommendations=core.recommendations,
            patterns=core.patterns,
            calculations=core.calculations,
            evidence=core.evidence,
            limitations=core.limitations,
            freshness=core.freshness,
            observations=core.observations,
            identity=str(identity),
        )


__all__ = [
    "CLAIM_DERIVED",
    "CLAIM_FACT",
    "CLAIM_NOT_CHECKABLE",
    "DECISION_PACK_SCHEMA",
    "DecisionIntelligence",
    "DecisionPack",
    "DecisionPackRequest",
    "DecisionSubject",
    "EvidenceRef",
]
