"""The analyst question catalog: one deterministic AnalystAnswer per known question (V7.6).

``AnalystIntelligence.analyze`` is a *typed* answer surface for future AI/automation: a caller
names a question id from the fixed catalog below and receives a plain-value answer whose
params, scope, output shape, evidence form, lifecycle and missing-value semantics are all
defined up front - so a machine can ask a well-formed question and check the answer without
this layer ever parsing language, scoring confidence, or inventing a question id.

What it is:

*   a **closed catalog of sixteen question ids** (:data:`QUESTION_IDS`), each with its
    contract in :data:`QUESTION_CATALOG`: which params it accepts, which scope keys it needs,
    what the output contains, how evidence is referenced, whether the underlying read is
    windowed or current-state, and what "missing" means for that question.  Unknown ids are
    refused with the list of real ids - never answered by best guess, never sentence-matched.
*   **reuse only**: every answer is assembled from the certified read paths the platform
    already has - the decision sections
    (:class:`~drilling_intelligence.intelligence.decision.DecisionIntelligence`), the
    comparison pack (:class:`~drilling_intelligence.intelligence.comparison.ComparisonIntelligence`),
    the operational aggregates of
    :class:`~drilling_intelligence.intelligence.field.FieldIntelligence` - including their
    evidence references, limitations and freshness vocabulary.  There is no second evidence
    implementation and no second fold.
*   **AI-ready shape without AI**: the answer is typed JSON (schema ``analyst-answer/1``)
    with a content identity hashed from its own payload.  No embeddings, no vectors, no RAG,
    no prompts, no model calls, no confidence scores.

What it is not:

*   not natural-language understanding: ``question`` is an exact catalog id.  A sentence is
    not a question here, and a caller who sends one gets a clear error listing the ids that
    exist.
*   not an inference layer: answers report what the repository records.  ``answer`` never
    contains advice, rankings, predictions or converted units; those live in no catalog
    entry because no repository method produces them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..core.errors import ValidationError
from ..core.hashing import sha256_obj
from ..database.models import Well
from .comparison import ComparisonIntelligence
from .decision import (
    FRESH_CURRENT,
    CalculationSection,
    DecisionIntelligence,
    EconomicsSection,
    EvidenceRef,
    ExecutionSection,
    LearningSection,
    OperationsSection,
    PatternSection,
    RecommendationSection,
    RiskSection,
    _scope_payload,
)
from .field import FieldIntelligence

#: Answer schema version, bumped when the JSON shape changes incompatibly.
ANALYST_SCHEMA = "analyst-answer/1"

#: The complete, closed question vocabulary - sixteen ids, spelled exactly.
QUESTION_IDS: tuple[str, ...] = (
    "well_profile",
    "npt_summary",
    "npt_by_category",
    "problem_summary",
    "problem_recurrence",
    "well_control_summary",
    "hse_summary",
    "plan_actual",
    "cost_summary",
    "risk_summary",
    "learning_summary",
    "pattern_summary",
    "calculation_status",
    "decision_pack",
    "well_comparison",
    "offset_comparison",
)

_SCOPE_KEYS = ("well_id", "field_id", "project_id", "program_id")


def _catalog_entry(
    question_id: str,
    *,
    params: Sequence[str],
    required_scope: Sequence[str],
    scope_keys: Sequence[str],
    output: str,
    evidence: str,
    lifecycle: str,
    missing: str,
    windowed: bool,
) -> dict[str, Any]:
    return {
        "question_id": question_id,
        "params": tuple(params),
        "required_scope": tuple(required_scope),
        "scope_keys": tuple(scope_keys),
        "output": output,
        "evidence": evidence,
        "lifecycle": lifecycle,
        "missing": missing,
        "windowed": bool(windowed),
    }


#: The catalog itself: every question's contract, in id order.  This is the machine-readable
#: half of the answer surface - a caller reads this to learn how to ask, then asks.
QUESTION_CATALOG: Mapping[str, Mapping[str, Any]] = {
    "well_profile": _catalog_entry(
        "well_profile",
        params=(),
        required_scope=("well_id",),
        scope_keys=("well_id",),
        output="the well's identity, field/project, spud date and its recorded section, NPT, problem, report and well-control counts",
        evidence="one method reference (FieldIntelligence.wells) with the well ids it read",
        lifecycle="current-state; no date window applies",
        missing="an unknown well is a ValidationError; a domain with no records contributes a zero count, while hour totals stay null when nothing stated them",
        windowed=False,
    ),
    "npt_summary": _catalog_entry(
        "npt_summary",
        params=(),
        required_scope=("exactly_one",),
        scope_keys=("well_id", "field_id", "project_id"),
        output="NPT rows, recorded hours, rows without a stated duration, undated rows and the by-category fold",
        evidence="aggregate method reference (FieldIntelligence.npt) with scope and window",
        lifecycle="windowed; since/until apply and undated rows stay outside the window",
        missing="rows=0 is a recorded zero; hours are summed only from rows that state a duration, and rows without one are counted, never zero-filled",
        windowed=True,
    ),
    "npt_by_category": _catalog_entry(
        "npt_by_category",
        params=(),
        required_scope=("exactly_one",),
        scope_keys=("well_id", "field_id", "project_id"),
        output="per-category records, hours, unknown durations, wells and first/last seen dates",
        evidence="aggregate method reference (FieldIntelligence.npt) with scope and window",
        lifecycle="windowed; since/until apply",
        missing="a category absent from the map has no records in scope; hours inside a category are stated sums only",
        windowed=True,
    ),
    "problem_summary": _catalog_entry(
        "problem_summary",
        params=(),
        required_scope=("exactly_one",),
        scope_keys=("well_id", "field_id", "project_id"),
        output="occurrences, affected wells, unattributed NPT problems and the by-type fold",
        evidence="aggregate method reference (FieldIntelligence.problems) with scope and window",
        lifecycle="windowed; since/until apply",
        missing="occurrences=0 means no problem rows in scope; unattributed hours stay unattributed rather than being assigned",
        windowed=True,
    ),
    "problem_recurrence": _catalog_entry(
        "problem_recurrence",
        params=(),
        required_scope=("exactly_one",),
        scope_keys=("well_id", "field_id", "project_id"),
        output="per-type occurrence counts, wells, sections, recorded hours and first/last seen dates",
        evidence="aggregate method reference (FieldIntelligence.problems) with scope and window",
        lifecycle="windowed; since/until apply",
        missing="recurrence is a count of recorded occurrences over recorded dates - a type with one row is not 'recurring', and no rate is computed",
        windowed=True,
    ),
    "well_control_summary": _catalog_entry(
        "well_control_summary",
        params=(),
        required_scope=("exactly_one",),
        scope_keys=("well_id", "field_id", "project_id"),
        output="well-control rows, event-type and severity folds, dated range, undated rows and the explicit-cause/pressure counters",
        evidence="aggregate method reference (FieldIntelligence.well_control) with scope and window, plus structured row ids",
        lifecycle="windowed; since/until apply",
        missing="events=0 means no well-control rows in scope; presence of rows is never a safety comparison",
        windowed=True,
    ),
    "hse_summary": _catalog_entry(
        "hse_summary",
        params=(),
        required_scope=("exactly_one",),
        scope_keys=("well_id", "field_id", "project_id"),
        output="incidents with well-scoped and site-scoped counters kept separate, type/severity folds, dated range and cause-wording counters",
        evidence="aggregate method reference (FieldIntelligence.hse) with scope and window, plus structured row ids",
        lifecycle="windowed; since/until apply",
        missing="a well scope never contains site-scoped rows (they are counted only in field/project scope as site_scoped_incidents); incidents=0 is a recorded zero",
        windowed=True,
    ),
    "plan_actual": _catalog_entry(
        "plan_actual",
        params=(),
        required_scope=("exactly_one",),
        scope_keys=("well_id", "program_id", "field_id", "project_id"),
        output="the plan/actual fold: rows, sections, per-status and per-metric counts, match modes (NAME, NAME_AMBIGUOUS, SECTION_ID, NO_MATCH) and the unit-incomparable rows",
        evidence="aggregate method reference (EngineeringRepository.plan_actual_summary) with the exact scope",
        lifecycle="current-state; plan_actual_summary accepts no date window",
        missing="NO_PLAN / NO_ACTUAL / NO_TARGET statuses are statements of absence, never zeros; INCOMPARABLE_UNITS rows keep both source values and refuse the subtraction",
        windowed=False,
    ),
    "cost_summary": _catalog_entry(
        "cost_summary",
        params=("detail",),
        required_scope=("exactly_one",),
        scope_keys=("well_id", "field_id", "project_id"),
        output="per-currency planned and actual totals with line counts, mixed-line and unpriced counters; optional wbs/cbs rollups at detail>=1",
        evidence="bounded cost row-id samples from the same statement summary() materialises",
        lifecycle="current-state; no date window applies",
        missing="a currency with no lines simply does not appear; totals are never summed or converted across currencies",
        windowed=False,
    ),
    "risk_summary": _catalog_entry(
        "risk_summary",
        params=(),
        required_scope=("exactly_one",),
        scope_keys=("well_id", "field_id", "project_id"),
        output="current and superseded risk counts, status fold, STATED/UNASSESSED severity fold, severity bands as recorded and evidence counters",
        evidence="bounded risk row-id samples",
        lifecycle="current-state (current rows plus the superseded counter); no date window applies",
        missing="severity null is UNASSESSED, never zero and never a score; no risk is ever ranked or compared across scales here",
        windowed=False,
    ),
    "learning_summary": _catalog_entry(
        "learning_summary",
        params=(),
        required_scope=("exactly_one",),
        scope_keys=("well_id", "field_id", "project_id"),
        output="lessons and practices with by-status folds and current/non-current counters, plus recommendations with status, links and evidence counters",
        evidence="bounded row-id samples for lessons (structured), practices and recommendations (structured)",
        lifecycle="current-state; current revisions are answered and non-current ones counted separately",
        missing="a status absent from a fold has zero rows; lessons, practices and recommendations are three different records and are never merged",
        windowed=False,
    ),
    "pattern_summary": _catalog_entry(
        "pattern_summary",
        params=(),
        required_scope=("exactly_one",),
        scope_keys=("well_id", "field_id", "project_id"),
        output="observed field patterns with status folds, occurrence/well counters and staleness",
        evidence="bounded field-pattern row-id samples",
        lifecycle="current-state; a pattern past stale_at reports stale as a limitation",
        missing="patterns are filed at field/project level - a well-scoped request reads its well's field patterns and says so in the section scope",
        windowed=False,
    ),
    "calculation_status": _catalog_entry(
        "calculation_status",
        params=(),
        required_scope=("exactly_one",),
        scope_keys=("well_id", "field_id", "project_id"),
        output="stored calculations with chain-decided current/history counts, status and method folds, input references and dependency states",
        evidence="bounded calculation row-id samples",
        lifecycle="current-state; current/history comes from the revision chain, never the status column; dependencies resolve through calculation_impact",
        missing="unresolved or stale dependencies are labelled UNRESOLVED/STALE, never assumed good; calculations are never executed by this question",
        windowed=False,
    ),
    "decision_pack": _catalog_entry(
        "decision_pack",
        params=("since", "until", "detail", "evidence_limit"),
        required_scope=("exactly_one",),
        scope_keys=("well_id", "field_id", "project_id"),
        output="the complete decision-pack/1 document (all eight sections, evidence, limitations, freshness, observations, identity)",
        evidence="the pack's own evidence references, unchanged",
        lifecycle="windowed where the underlying section accepts a window; each section states its own scope",
        missing="the pack's limitation vocabulary carries every absence: missing_plan, missing_actual, unknown_npt_duration, unassessed_risk and the rest",
        windowed=True,
    ),
    "well_comparison": _catalog_entry(
        "well_comparison",
        params=("well_ids", "since", "until", "detail", "evidence_limit"),
        required_scope=("well_ids",),
        scope_keys=("well_ids",),
        output="the complete well-comparison/1 document (basis, sections, evidence, limitations, freshness, observations, identity)",
        evidence="the pack's own evidence references, unchanged",
        lifecycle="windowed where the underlying section accepts a window; basis states the window and undated behaviour",
        missing="the comparison's value states carry every absence: MISSING, NO_RECORDS, UNASSESSED - never a zero substitution",
        windowed=True,
    ),
    "offset_comparison": _catalog_entry(
        "offset_comparison",
        params=("anchor", "offsets", "since", "until", "detail", "evidence_limit", "offset_limit"),
        required_scope=("anchor",),
        scope_keys=("anchor", "offsets"),
        output="the complete well-comparison/1 document whose basis kind is offset_candidates (or explicit_wells when offsets are named)",
        evidence="the pack's own evidence references, unchanged",
        lifecycle="windowed where the underlying section accepts a window; discovery reuses FieldIntelligence.offset_candidates",
        missing="no recorded candidates for an anchor is a ValidationError - the scope is never broadened to other fields",
        windowed=True,
    ),
}


@dataclass(frozen=True)
class AnalystRequest:
    """The exact request that produced an answer - kept so it can be re-run and compared."""

    question: str = ""
    well_id: str = ""
    field_id: str = ""
    project_id: str = ""
    program_id: str = ""
    well_ids: tuple[str, ...] = ()
    anchor: str = ""
    offsets: tuple[str, ...] = ()
    since: Any = None
    until: Any = None
    detail: int = 1
    evidence_limit: int = 10
    offset_limit: int = 10

    def payload(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "well_id": self.well_id,
            "field_id": self.field_id,
            "project_id": self.project_id,
            "program_id": self.program_id,
            "well_ids": list(self.well_ids),
            "anchor": self.anchor,
            "offsets": list(self.offsets),
            "since": (self.since.isoformat() if hasattr(self.since, "isoformat") else self.since),
            "until": (self.until.isoformat() if hasattr(self.until, "isoformat") else self.until),
            "detail": int(self.detail),
            "evidence_limit": int(self.evidence_limit),
            "offset_limit": int(self.offset_limit),
        }


@dataclass(frozen=True)
class AnalystAnswer:
    """One deterministic answer for one catalog question, as plain values."""

    schema: str
    question_id: str
    request: AnalystRequest
    scope: Mapping[str, Any]
    answer: Mapping[str, Any]
    evidence: tuple[EvidenceRef, ...]
    limitations: tuple[str, ...]
    freshness: Mapping[str, str]
    observations: tuple[str, ...]
    identity: str

    def to_dict(self) -> dict[str, Any]:
        """The stable machine-readable form.  Key order follows the dataclass fields."""
        return {
            "schema": self.schema,
            "question_id": self.question_id,
            "request": self.request.payload(),
            "scope": dict(self.scope),
            "answer": dict(self.answer),
            "evidence": [ref.payload() for ref in self.evidence],
            "limitations": list(self.limitations),
            "freshness": dict(self.freshness),
            "observations": list(self.observations),
            "identity": self.identity,
        }


def _empty_execution() -> ExecutionSection:
    return ExecutionSection(
        scope=_scope_payload(),
        claim_kind="FACT",
        sections=0,
        rows=0,
        sections_with_target=0,
        sections_without_target=0,
        sections_without_actual=0,
        by_status={},
        by_matched_by={},
        by_metric={},
        incomparable=(),
    )


def _empty_operations() -> OperationsSection:
    return OperationsSection(
        scope=_scope_payload(),
        claim_kind="FACT",
        npt={},
        problems={},
        well_control={},
        hse={},
    )


def _empty_economics() -> EconomicsSection:
    return EconomicsSection(
        scope=_scope_payload(), claim_kind="FACT", summary={}, wbs_rollup=(), cbs_rollup=()
    )


def _empty_risk() -> RiskSection:
    return RiskSection(
        scope=_scope_payload(),
        claim_kind="FACT",
        current=0,
        superseded=0,
        by_status={},
        by_scope_level={},
        stated={},
        severity_bands={},
        with_evidence=0,
        without_evidence=0,
        relations={},
    )


def _empty_learning() -> LearningSection:
    return LearningSection(scope=_scope_payload(), claim_kind="FACT", lessons={}, practices={})


def _empty_recommendations() -> RecommendationSection:
    return RecommendationSection(
        scope=_scope_payload(),
        claim_kind="FACT",
        total=0,
        by_status={},
        links={},
        with_evidence=0,
        without_evidence=0,
    )


def _empty_patterns() -> PatternSection:
    return PatternSection(
        scope=_scope_payload(),
        claim_kind="FACT",
        total=0,
        by_status={},
        stale=0,
        with_evidence=0,
        without_evidence=0,
    )


def _empty_calculations() -> CalculationSection:
    return CalculationSection(
        scope=_scope_payload(),
        claim_kind="FACT",
        total=0,
        current=0,
        history=0,
        by_status={},
        by_method={},
        inputs=0,
        subjects_resolved=0,
        subjects_truncated=False,
        dependency={},
        dependency_entries=(),
    )


class AnalystIntelligence(FieldIntelligence):
    """Builds :class:`AnalystAnswer` values for exact catalog questions."""

    # -- validation ---------------------------------------------------------------------

    @staticmethod
    def _catalog(question: str) -> Mapping[str, Any]:
        key = str(question or "").strip()
        entry = QUESTION_CATALOG.get(key)
        if entry is None:
            known = ", ".join(QUESTION_IDS)
            raise ValidationError(
                f"unknown question {question!r}",
                hint=f"known questions: {known}",
            )
        return entry

    @staticmethod
    def _scope_for(
        entry: Mapping[str, Any],
        *,
        well_id: str,
        field_id: str,
        project_id: str,
        program_id: str,
        well_ids: Sequence[str],
        anchor: str,
        offsets: Sequence[str],
        since: Any,
        until: Any,
    ) -> None:
        """Refuse anything the catalog entry does not describe - never broaden, never
        guess which of two scopes was probably meant."""
        required = tuple(entry["required_scope"])
        allowed = tuple(entry["scope_keys"])
        given_scopes = {
            key: value
            for key, value in (
                ("well_id", well_id),
                ("field_id", field_id),
                ("project_id", project_id),
                ("program_id", program_id),
            )
            if str(value or "").strip()
        }
        # params outside the catalog entry are refused first, not ignored: a caller who
        # names program_id for a question that has no program concept gets that answer,
        # not a confusing complaint about how many scopes they passed.
        extras = set(given_scopes) - set(allowed)
        if extras:
            raise ValidationError(
                f"{entry['question_id']} does not accept scope {', '.join(sorted(extras))}",
                hint="the catalog entry lists the scope keys this question takes",
            )
        if required == ("exactly_one",):
            if len(given_scopes) != 1:
                raise ValidationError(
                    f"{entry['question_id']} needs exactly one of well_id, field_id, project_id",
                    hint="name one scope and only one",
                )
        elif "scope" in required:
            if not any(key in given_scopes for key in allowed if key != "program_id") and not (
                "program_id" in allowed and "program_id" in given_scopes
            ):
                raise ValidationError(
                    f"{entry['question_id']} needs a scope: {', '.join(allowed)}",
                    hint="pass at least one of the scope keys the catalog lists",
                )
        elif required:
            for key in required:
                if key in ("well_ids",):
                    if len([value for value in well_ids if str(value or "").strip()]) < 2 and not (
                        str(anchor or "").strip()
                    ):
                        raise ValidationError(
                            f"{entry['question_id']} needs at least two well ids, or an anchor",
                            hint="pass --well twice, or --anchor with --offsets",
                        )
                elif key == "anchor":
                    if not str(anchor or "").strip():
                        raise ValidationError(
                            f"{entry['question_id']} needs an anchor well",
                            hint="pass --anchor <well>",
                        )
                elif key not in given_scopes:
                    raise ValidationError(
                        f"{entry['question_id']} needs {key}",
                        hint=f"pass --{key.replace('_', '-')}",
                    )
        if (since is not None or until is not None) and not entry["windowed"]:
            raise ValidationError(
                f"{entry['question_id']} does not accept a date window",
                hint="the underlying read is current-state; drop --since/--until",
            )

    # -- answers -------------------------------------------------------------------------

    def analyze(
        self,
        question: str,
        *,
        well_id: str = "",
        field_id: str = "",
        project_id: str = "",
        program_id: str = "",
        well_ids: Sequence[str] = (),
        anchor: str = "",
        offsets: Sequence[str] = (),
        since: Any = None,
        until: Any = None,
        detail: int = 1,
        evidence_limit: int = 10,
        offset_limit: int = 10,
    ) -> AnalystAnswer:
        """Answer one exact catalog question, deterministically and read-only."""
        entry = self._catalog(question)
        self._scope_for(
            entry,
            well_id=well_id,
            field_id=field_id,
            project_id=project_id,
            program_id=program_id,
            well_ids=well_ids,
            anchor=anchor,
            offsets=offsets,
            since=since,
            until=until,
        )
        request = AnalystRequest(
            question=str(entry["question_id"]),
            well_id=str(well_id or "").strip(),
            field_id=str(field_id or "").strip(),
            project_id=str(project_id or "").strip(),
            program_id=str(program_id or "").strip(),
            well_ids=tuple(str(value).strip() for value in well_ids if str(value).strip()),
            anchor=str(anchor or "").strip(),
            offsets=tuple(str(value).strip() for value in offsets if str(value).strip()),
            since=since,
            until=until,
            detail=max(int(detail), 0),
            evidence_limit=max(int(evidence_limit), 0),
            offset_limit=max(int(offset_limit), 1),
        )
        scope = {
            "well_id": (request.well_id or None),
            "field_id": (request.field_id or None),
            "project_id": (request.project_id or None),
            "program_id": (request.program_id or None),
            "well_ids": list(request.well_ids),
            "anchor": (request.anchor or None),
            "offsets": list(request.offsets),
            "since": (
                request.since.isoformat() if hasattr(request.since, "isoformat") else request.since
            ),
            "until": (
                request.until.isoformat() if hasattr(request.until, "isoformat") else request.until
            ),
        }

        dispatch = {
            "well_profile": self._well_profile,
            "npt_summary": self._ops_question,
            "npt_by_category": self._ops_question,
            "problem_summary": self._ops_question,
            "problem_recurrence": self._ops_question,
            "well_control_summary": self._ops_question,
            "hse_summary": self._ops_question,
            "plan_actual": self._plan_actual,
            "cost_summary": self._cost_summary,
            "risk_summary": self._risk_summary,
            "learning_summary": self._learning_summary,
            "pattern_summary": self._pattern_summary,
            "calculation_status": self._calculation_status,
            "decision_pack": self._decision_pack,
            "well_comparison": self._comparison,
            "offset_comparison": self._comparison,
        }
        answer, evidence, sections, extra_observations, passthrough = dispatch[
            str(entry["question_id"])
        ](entry, request)

        decision = DecisionIntelligence(self.session)
        if passthrough is not None:
            # Pack questions pass their own limitation/freshness/observation sets through
            # unchanged: recomputing them here would be a second implementation.
            limitations = list(passthrough.get("limitations", ()))
            freshness = dict(passthrough.get("freshness", {}))
            observations = list(extra_observations)
        else:
            execution = sections.get("execution") or _empty_execution()
            operations = sections.get("operations") or _empty_operations()
            economics = sections.get("economics") or _empty_economics()
            risk = sections.get("risk") or _empty_risk()
            learning = sections.get("learning") or _empty_learning()
            recommendations = sections.get("recommendations") or _empty_recommendations()
            patterns = sections.get("patterns") or _empty_patterns()
            calculations = sections.get("calculations") or _empty_calculations()

            limitations = list(
                decision._limitations(
                    execution=execution,
                    operations=operations,
                    economics=economics,
                    risk=risk,
                    recommendations=recommendations,
                    patterns=patterns,
                    calculations=calculations,
                    evidence=evidence,
                )
            )
            freshness = dict(
                decision._freshness(
                    execution=execution,
                    operations=operations,
                    economics=economics,
                    risk=risk,
                    learning=learning,
                    recommendations=recommendations,
                    patterns=patterns,
                    calculations=calculations,
                )
            )
            observations = list(extra_observations)
        freshness["question"] = FRESH_CURRENT

        core = AnalystAnswer(
            schema=ANALYST_SCHEMA,
            question_id=str(entry["question_id"]),
            request=request,
            scope=scope,
            answer=answer,
            evidence=tuple(evidence),
            limitations=tuple(limitations),
            freshness=freshness,
            observations=tuple(observations),
            identity="",  # filled below from the payload itself
        )
        identity = sha256_obj(
            {
                "schema": core.schema,
                "question_id": core.question_id,
                "request": core.request.payload(),
                "scope": dict(core.scope),
                "answer": dict(core.answer),
                "limitations": list(core.limitations),
                "freshness": dict(core.freshness),
                "observations": list(core.observations),
            }
        )
        return AnalystAnswer(
            schema=core.schema,
            question_id=core.question_id,
            request=core.request,
            scope=core.scope,
            answer=core.answer,
            evidence=core.evidence,
            limitations=core.limitations,
            freshness=core.freshness,
            observations=core.observations,
            identity=str(identity),
        )

    # -- per-question builders -----------------------------------------------------------

    def _scope_kwargs(self, request: AnalystRequest) -> dict[str, str]:
        return {
            "well_id": request.well_id,
            "field_id": request.field_id,
            "project_id": request.project_id,
        }

    def _window_scope(self, request: AnalystRequest) -> dict[str, Any]:
        scope = _scope_payload(
            well_id=request.well_id,
            field_id=request.field_id,
            project_id=request.project_id,
        )
        scope["since"] = (
            request.since.isoformat() if hasattr(request.since, "isoformat") else request.since
        )
        scope["until"] = (
            request.until.isoformat() if hasattr(request.until, "isoformat") else request.until
        )
        return scope

    def _well_profile(
        self, entry: Mapping[str, Any], request: AnalystRequest
    ) -> tuple[dict[str, Any], list[EvidenceRef], dict[str, Any], list[str], None]:
        row = self.session.get(Well, request.well_id)
        if row is None:
            raise ValidationError(
                f"unknown well: {request.well_id}",
                hint="the id must exist in this workspace",
            )
        profile: dict[str, Any] = {
            "id": str(row.id),
            "name": str(row.name),
            "field_id": (str(row.field_id) if row.field_id else None),
            "project_id": (str(row.project_id) if row.project_id else None),
            "spud_date": (row.spud_date.isoformat() if row.spud_date else None),
        }
        counted = None
        if row.field_id:
            counted = self.wells(field_id=str(row.field_id))
        elif row.project_id:
            counted = self.wells(project_id=str(row.project_id))
        if counted is not None:
            match = next(
                (item for item in counted.get("wells", []) if item.get("id") == str(row.id)),
                None,
            )
            if match:
                profile = dict(match)
        answer = {"wells": [profile], "count": 1}
        evidence = [
            EvidenceRef(
                section="analyst",
                domain="well",
                kind="aggregate",
                identity="method",
                count=1,
                method="FieldIntelligence.wells",
                scope=_scope_payload(well_id=request.well_id),
            )
        ]
        observations = [
            f"1 well in scope: {profile.get('name')} "
            f"(spud {profile.get('spud_date') or 'not recorded'})."
        ]
        return answer, evidence, {}, observations, None

    def _ops_question(
        self, entry: Mapping[str, Any], request: AnalystRequest
    ) -> tuple[dict[str, Any], list[EvidenceRef], dict[str, Any], list[str]]:
        decision = DecisionIntelligence(self.session)
        operations = decision._operations(
            scope_key=next(
                key for key in ("well_id", "field_id", "project_id") if getattr(request, key)
            ),
            scope_value=next(
                getattr(request, key)
                for key in ("well_id", "field_id", "project_id")
                if getattr(request, key)
            ),
            since=request.since,
            until=request.until,
        )
        question = str(entry["question_id"])
        slices = {
            "npt_summary": ("npt", operations.npt, "FieldIntelligence.npt", "npt_record", "rows"),
            "npt_by_category": (
                "npt_by_category",
                {"by_category": operations.npt.get("by_category", {})},
                "FieldIntelligence.npt",
                "npt_record",
                "rows",
            ),
            "problem_summary": (
                "problems",
                operations.problems,
                "FieldIntelligence.problems",
                "problem_occurrence",
                "occurrences",
            ),
            "problem_recurrence": (
                "problem_recurrence",
                {"by_type": operations.problems.get("by_type", {})},
                "FieldIntelligence.problems",
                "problem_occurrence",
                "occurrences",
            ),
            "well_control_summary": (
                "well_control",
                operations.well_control,
                "FieldIntelligence.well_control",
                "well_control_event",
                "events",
            ),
            "hse_summary": (
                "hse",
                operations.hse,
                "FieldIntelligence.hse",
                "hse_incident",
                "incidents",
            ),
        }
        key, payload, method, domain, count_key = slices[question]
        answer = {key: dict(payload)}
        count = int(payload.get(count_key, 0) or 0)
        evidence = [
            EvidenceRef(
                section="analyst",
                domain=domain,
                kind="aggregate",
                identity="method",
                count=count,
                method=method,
                scope=self._window_scope(request),
            )
        ]
        if question == "npt_summary":
            observations = [
                f"{payload.get('rows', 0)} NPT rows, {payload.get('total_hours')} h recorded, "
                f"{payload.get('unknown_duration', 0)} rows without a stated duration, "
                f"{payload.get('undated', 0)} undated."
            ]
        elif question == "npt_by_category":
            by_category = payload.get("by_category") or {}
            observations = [
                "NPT hours by category: "
                + (
                    ", ".join(
                        f"{name} {entry.get('hours')}"
                        for name, entry in sorted(by_category.items())
                    )
                    or "none"
                )
                + "."
            ]
        elif question == "problem_summary":
            observations = [
                f"{payload.get('occurrences', 0)} problem occurrences across "
                f"{len(payload.get('by_type') or {})} recorded types; "
                f"{payload.get('unattributed_npt_problems', 0)} with unattributed NPT hours."
            ]
        elif question == "problem_recurrence":
            by_type = payload.get("by_type") or {}
            observations = [
                "Occurrences by type: "
                + (
                    ", ".join(
                        f"{name} {entry.get('occurrences')} "
                        f"({entry.get('first_seen_at')} to {entry.get('last_seen_at')})"
                        for name, entry in sorted(by_type.items())
                    )
                    or "none"
                )
                + "."
            ]
        elif question == "well_control_summary":
            observations = [
                f"{payload.get('events', 0)} well-control rows, "
                f"{payload.get('undated', 0)} undated, "
                f"{payload.get('with_explicit_cause', 0)} with a source-stated cause."
            ]
        else:
            site = int(payload.get("site_scoped_incidents", 0) or 0)
            observations = [
                f"{payload.get('incidents', 0)} HSE incidents "
                f"({payload.get('well_scoped_incidents', 0)} well-scoped, {site} site-scoped), "
                f"{payload.get('undated', 0)} undated."
            ]
            if site:
                observations.append(
                    f"{site} site-scoped rows are counted in this scope and never attached "
                    "to a well."
                )
        return answer, evidence, {"operations": operations}, observations, None

    def _plan_actual(
        self, entry: Mapping[str, Any], request: AnalystRequest
    ) -> tuple[dict[str, Any], list[EvidenceRef], dict[str, Any], list[str]]:
        decision = DecisionIntelligence(self.session)
        scope_key, scope_value = next(
            (key, getattr(request, key))
            for key in ("well_id", "program_id", "field_id", "project_id")
            if getattr(request, key)
        )
        execution, section_ids = decision._execution(
            scope_key=scope_key, scope_value=scope_value, detail=max(int(request.detail), 1)
        )
        answer = dict(execution.payload())
        evidence = list(
            decision._execution_evidence(execution, section_ids, int(request.evidence_limit))
        )
        observations = [
            f"{execution.rows} plan/actual rows across {execution.sections} sections; "
            "statuses: "
            + (
                ", ".join(f"{key} {value}" for key, value in sorted(execution.by_status.items()))
                or "none"
            )
            + "."
        ]
        return answer, evidence, {"execution": execution}, observations, None

    def _cost_summary(
        self, entry: Mapping[str, Any], request: AnalystRequest
    ) -> tuple[dict[str, Any], list[EvidenceRef], dict[str, Any], list[str]]:
        decision = DecisionIntelligence(self.session)
        scope_key = next(
            key for key in ("well_id", "field_id", "project_id") if getattr(request, key)
        )
        economics = decision._economics(
            scope_key=scope_key,
            scope_value=getattr(request, scope_key),
            detail=max(int(request.detail), 1),
        )
        evidence = list(
            decision._cost_evidence(
                scope_key=scope_key,
                scope_value=getattr(request, scope_key),
                evidence_limit=int(request.evidence_limit),
            )
        )
        summary = economics.summary
        by_currency = summary.get("by_currency") or {}
        answer = {
            "summary": dict(summary),
            "wbs_rollup": [dict(row) for row in economics.wbs_rollup],
            "cbs_rollup": [dict(row) for row in economics.cbs_rollup],
        }
        currencies = sorted(by_currency)
        observations = [
            (
                f"cost lines in {', '.join(currencies)}: "
                + ", ".join(
                    f"{code} {int((by_currency[code] or {}).get('lines', 0))} lines"
                    for code in currencies
                )
                + "; planned and actual stay separate per currency."
            )
            if currencies
            else "no cost lines in scope."
        ]
        if len(currencies) > 1:
            observations.append(
                "No total is reported across currencies and no currency is converted."
            )
        return answer, evidence, {"economics": economics}, observations, None

    def _risk_summary(
        self, entry: Mapping[str, Any], request: AnalystRequest
    ) -> tuple[dict[str, Any], list[EvidenceRef], dict[str, Any], list[str]]:
        decision = DecisionIntelligence(self.session)
        scope_key = next(
            key for key in ("well_id", "field_id", "project_id") if getattr(request, key)
        )
        scope_value = getattr(request, scope_key)
        wells = self._wells(
            field_id=request.field_id,
            project_id=request.project_id,
            well_id=request.well_id,
        )
        risk, evidence = decision._risk(
            scope_key=scope_key,
            scope_value=scope_value,
            wells=wells,
            evidence_limit=int(request.evidence_limit),
        )
        unassessed = int(risk.stated.get("severity_unassessed", 0))
        observations = [
            f"{risk.current} current and {risk.superseded} superseded risks; "
            f"{unassessed} with no source-stated severity."
        ]
        return dict(risk.payload()), evidence, {"risk": risk}, observations, None

    def _learning_summary(
        self, entry: Mapping[str, Any], request: AnalystRequest
    ) -> tuple[dict[str, Any], list[EvidenceRef], dict[str, Any], list[str]]:
        decision = DecisionIntelligence(self.session)
        scope_key = next(
            key for key in ("well_id", "field_id", "project_id") if getattr(request, key)
        )
        scope_value = getattr(request, scope_key)
        wells = self._wells(
            field_id=request.field_id,
            project_id=request.project_id,
            well_id=request.well_id,
        )
        learning, recommendations, evidence = decision._learning(
            scope_key=scope_key,
            scope_value=scope_value,
            wells=wells,
            evidence_limit=int(request.evidence_limit),
        )
        answer = {
            "learning": dict(learning.payload()),
            "recommendations": dict(recommendations.payload()),
        }
        observations = [
            f"{learning.lessons.get('approved', 0)} approved lessons, "
            f"{learning.practices.get('adopted', 0)} adopted practices, "
            f"{recommendations.total} recommendations "
            f"({recommendations.by_status.get('PROPOSED', 0)} proposed, "
            f"{recommendations.by_status.get('ACCEPTED', 0)} accepted)."
        ]
        return (
            answer,
            evidence,
            {"learning": learning, "recommendations": recommendations},
            observations,
            None,
        )

    def _pattern_summary(
        self, entry: Mapping[str, Any], request: AnalystRequest
    ) -> tuple[dict[str, Any], list[EvidenceRef], dict[str, Any], list[str]]:
        decision = DecisionIntelligence(self.session)
        scope_key = next(
            key for key in ("well_id", "field_id", "project_id") if getattr(request, key)
        )
        scope_value = getattr(request, scope_key)
        patterns, evidence = decision._patterns(
            scope_key=scope_key,
            scope_value=scope_value,
            subject=self._decision_subject(self.session, scope_key, scope_value),
            evidence_limit=int(request.evidence_limit),
        )
        observations = [
            f"{patterns.total} observed patterns ({patterns.stale} stale); descriptive counts only."
        ]
        return dict(patterns.payload()), evidence, {"patterns": patterns}, observations, None

    @staticmethod
    def _decision_subject(session: Any, scope_key: str, scope_value: str) -> Any:
        from .decision import DecisionSubject

        if scope_key != "well_id":
            kind = "field" if scope_key == "field_id" else "project"
            return DecisionSubject(kind=kind, id=scope_value, name=scope_value)
        row = session.get(Well, scope_value)
        return DecisionSubject(
            kind="well",
            id=scope_value,
            name=(str(row.name) if row else ""),
            field_id=(str(row.field_id) if row and row.field_id else None),
            project_id=(str(row.project_id) if row and row.project_id else None),
        )

    def _calculation_status(
        self, entry: Mapping[str, Any], request: AnalystRequest
    ) -> tuple[dict[str, Any], list[EvidenceRef], dict[str, Any], list[str]]:
        decision = DecisionIntelligence(self.session)
        scope_key = next(
            key for key in ("well_id", "field_id", "project_id") if getattr(request, key)
        )
        scope_value = getattr(request, scope_key)
        wells = self._wells(
            field_id=request.field_id,
            project_id=request.project_id,
            well_id=request.well_id,
        )
        calculations, evidence = decision._calculations(
            scope_key=scope_key,
            scope_value=scope_value,
            wells=wells,
            evidence_limit=int(request.evidence_limit),
        )
        observations = [
            f"{calculations.total} stored calculations: {calculations.current} current, "
            f"{calculations.history} history; "
            f"{int(calculations.dependency.get('STALE', 0))} stale and "
            f"{int(calculations.dependency.get('UNRESOLVED', 0))} unresolved dependencies; "
            "none executed."
        ]
        return (
            dict(calculations.payload()),
            evidence,
            {"calculations": calculations},
            observations,
            None,
        )

    def _decision_pack(
        self, entry: Mapping[str, Any], request: AnalystRequest
    ) -> tuple[dict[str, Any], list[EvidenceRef], dict[str, Any], list[str]]:
        pack = DecisionIntelligence(self.session).pack(
            well_id=request.well_id,
            field_id=request.field_id,
            project_id=request.project_id,
            since=request.since,
            until=request.until,
            detail=max(int(request.detail), 1),
            evidence_limit=int(request.evidence_limit),
        )
        payload = pack.to_dict()
        # The pack carries its own limitations/freshness/observations; the analyst layer
        # passes them through unchanged rather than re-deriving them a second time.
        passthrough = {
            "limitations": list(pack.limitations),
            "freshness": dict(pack.freshness),
        }
        return payload, list(pack.evidence), {}, list(pack.observations), passthrough

    def _comparison(
        self, entry: Mapping[str, Any], request: AnalystRequest
    ) -> tuple[dict[str, Any], list[EvidenceRef], dict[str, Any], list[str]]:
        comparison = ComparisonIntelligence(self.session)
        if str(entry["question_id"]) == "well_comparison":
            pack = comparison.compare(
                well_ids=request.well_ids,
                since=request.since,
                until=request.until,
                detail=max(int(request.detail), 1),
                evidence_limit=int(request.evidence_limit),
            )
        else:
            pack = comparison.compare(
                anchor=request.anchor,
                offsets=request.offsets,
                since=request.since,
                until=request.until,
                detail=max(int(request.detail), 1),
                evidence_limit=int(request.evidence_limit),
                offset_limit=max(int(request.offset_limit), 1),
            )
        payload = pack.to_dict()
        passthrough = {
            "limitations": list(pack.limitations),
            "freshness": dict(pack.freshness),
        }
        return payload, list(pack.evidence), {}, list(pack.observations), passthrough


__all__ = [
    "ANALYST_SCHEMA",
    "QUESTION_CATALOG",
    "QUESTION_IDS",
    "AnalystAnswer",
    "AnalystIntelligence",
    "AnalystRequest",
]
