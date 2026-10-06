"""The cross-well comparison read model: one deterministic ComparisonPack per selection (V7.6).

``ComparisonPack`` answers one comparative engineering question - *how do these wells differ on
the same recorded basis, what is actually comparable between them, what is missing, and what
evidence backs each number* - as a read-only, re-runnable, plain-value projection.

What it is:

*   a **matrix of per-subject numbers, never a score**: every metric row carries one value per
    subject with its own ``value_state`` and ``comparability``, plus an overall verdict from
    ``COMPARABLE`` / ``INCOMPARABLE`` / ``MISSING`` / ``NOT_APPLICABLE`` / ``UNRESOLVED`` /
    ``STALE``.  A value that is absent stays absent (``value: null`` with a state that says
    *why*); it never becomes zero, and no missing value is ever averaged or imputed.
*   **reuse of the certified read paths**: per subject, the pack folds the exact aggregates the
    platform already certifies - :class:`~drilling_intelligence.intelligence.decision.
    DecisionIntelligence`'s well-scoped sections (which are themselves the V7.3A operational
    aggregates, the plan/actual fold, the cost summary, and the risk/learning/pattern/
    calculation folds) - and
    :meth:`~drilling_intelligence.intelligence.field.FieldIntelligence.offset_candidates` for
    candidate discovery.  Nothing is re-derived, no arithmetic is rebuilt, and there is only
    one evidence implementation: the decision pack's own references are reused verbatim.
*   **units and scales are facts, not obstacles**: numbers in different units (USD vs NOK,
    psi vs bar, m vs ft, d vs h, ppg vs sg) are reported side by side with
    ``comparability: INCOMPARABLE`` and never merged; risk severity bands are comparable only
    while the subjects' recorded risk-scale identities match, otherwise ``UNRESOLVED``.

What it is not:

*   not a prediction, score, ranking, safety comparison, "best offset", optimization or
    recommendation: the pack reports differences the repository states, and stops there.
    Shared problem types are presented as shared recorded categories - never as a cause of
    anything.  Currency totals are never summed across currencies and never converted, and
    record presence (well-control rows, HSE incidents, lessons) is never a ranking of who is
    safer or better.
*   not a new store: read-side only, no tables, no cache, no persisted aggregate.  The pack
    carries a content identity hashed from its own payload (schema, request, basis, subjects,
    metrics, states - no timestamps, no rankings), so the same request over the same state
    always produces the same identity.

Evidence uses the same two honest forms as the decision pack: ``structured:<record-type>:
<row-id>`` identities for searchable domains, bounded row-id samples for domains without a
search projection, and exact re-executable method + scope + window references for windowed
aggregates.  Sections are per subject and never merged: two wells' plan/actual rows are two
statements, displayed separately.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from sqlalchemy import func, select

from ..core.enums import RecommendationLifecycle, RiskLifecycle
from ..core.errors import ValidationError
from ..core.hashing import sha256_obj
from ..database.models import (
    HseIncident,
    NptRecord,
    ProblemOccurrence,
    RiskRecord,
    Well,
    WellControlEvent,
)
from ..knowledge.repository import KnowledgeRepository
from .decision import (
    CLAIM_DERIVED,
    CLAIM_FACT,
    CLAIM_NOT_CHECKABLE,
    FRESH_CURRENT,
    FRESH_NOT_APPLICABLE,
    FRESH_NOT_AVAILABLE,
    FRESH_STALE,
    FRESH_UNRESOLVED,
    CalculationSection,
    DecisionIntelligence,
    DecisionSubject,
    EvidenceRef,
    LearningSection,
    PatternSection,
    RecommendationSection,
    RiskSection,
    _scope_payload,
)
from .field import FieldIntelligence

#: Pack schema version, bumped when the JSON shape changes incompatibly.
COMPARISON_SCHEMA = "well-comparison/1"

# -- the comparability vocabulary -------------------------------------------------------

COMPARABLE = "COMPARABLE"
INCOMPARABLE = "INCOMPARABLE"
MISSING = "MISSING"
NOT_APPLICABLE = "NOT_APPLICABLE"
UNRESOLVED = "UNRESOLVED"
STALE = "STALE"

#: The complete set a metric's overall ``comparability`` may take.
COMPARABILITY_STATES = (
    COMPARABLE,
    INCOMPARABLE,
    MISSING,
    NOT_APPLICABLE,
    UNRESOLVED,
    STALE,
)

# -- the value-state vocabulary (per subject) -------------------------------------------

#: The source states this value for this subject.
VALUE_STATED = "STATED"
#: A count that is itself the fact: a count of rows in scope is 0 when there are none, and
#: "zero recorded rows" must never read as "no data".
VALUE_COUNTED = "COUNTED"
#: The value is a sum over rows, some of which state no number: the total is real but does not
#: cover every row, and the uncovered count travels with it (it is never silently zero).
VALUE_PARTIAL = "PARTIAL"
#: Rows exist for this subject but the source never stated the value on any of them - not
#: zero, not missing rows: "recorded, unvalued".
VALUE_UNASSESSED = "UNASSESSED"
#: The domain has no records for this subject at all - deliberately not zero (no NPT rows and
#: 0.0 h of NPT are different statements) and deliberately not MISSING (the domain was read).
VALUE_NO_RECORDS = "NO_RECORDS"
#: The metric does not apply to this subject by construction.
VALUE_NOT_APPLICABLE = "NOT_APPLICABLE"
#: A staleness marker the source itself carries on every value of this row.
VALUE_STALE = "STALE"

#: Value states that are real numbers a comparison can use (units permitting).
_VALUE_COMPARABLE_STATES = (VALUE_STATED, VALUE_PARTIAL, VALUE_COUNTED)

#: How many offset candidate detail profiles the pack ever materialises.  Discovery itself is
#: bounded by the request's ``offset_limit``; this cap bounds the *profile* payload so a wide
#: request stays a bounded document.  Exceeding it is reported through ``truncated_detail``.
OFFSET_PROFILE_CAP = 16

#: Domains whose certified methods accept a date window (the others are current-state reads
#: and say so in their detail rather than pretending the window applied).
WINDOWED_DOMAINS = ("npt", "problems", "well_control", "hse")

#: The documented undated-record behaviour, carried on every pack that uses a window.
UNDATED_BEHAVIOR = (
    "undated rows are excluded from windowed counts and reported separately in each "
    "subject's undated counter; they are never assumed inside the window"
)

#: Deterministic precedence when subjects disagree about a section's freshness: the pack takes
#: the least-fresh reading across subjects (the most restrictive state wins).
_FRESHNESS_RANK = {
    FRESH_UNRESOLVED: 4,
    FRESH_STALE: 3,
    FRESH_NOT_AVAILABLE: 2,
    FRESH_NOT_APPLICABLE: 1,
    FRESH_CURRENT: 0,
}


def _iso(value: Any) -> Any:
    """A date/datetime as its stable ISO string, anything else untouched."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------------------
# Contracts
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ComparisonRequest:
    """The exact request that produced a pack - kept so a pack can be re-run and compared."""

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
            "well_ids": list(self.well_ids),
            "anchor": self.anchor,
            "offsets": list(self.offsets),
            "since": _iso(self.since),
            "until": _iso(self.until),
            "detail": int(self.detail),
            "evidence_limit": int(self.evidence_limit),
            "offset_limit": int(self.offset_limit),
        }


@dataclass(frozen=True)
class SubjectIdentity:
    """Who one column of the matrix is, and why it is in the selection."""

    well_id: str
    name: str
    selection: str  # "explicit" | "anchor" | "named_offset" | "discovered_offset"
    field_id: str | None = None
    project_id: str | None = None
    spud_date: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "well_id": self.well_id,
            "name": self.name,
            "selection": self.selection,
            "field_id": self.field_id,
            "project_id": self.project_id,
            "spud_date": self.spud_date,
        }


@dataclass(frozen=True)
class ComparisonBasis:
    """Why these subjects are on the same page: how they were selected and on what shared
    recorded basis (problem types, hole sizes) the comparison may rest.

    ``kind`` is ``explicit_wells`` when the caller named every well (or named the offsets
    themselves), ``offset_candidates`` when the offsets came from
    :meth:`~drilling_intelligence.intelligence.field.FieldIntelligence.offset_candidates`.
    Discovered candidate rows keep the field names that method returns (``shared_problem_types``,
    ``shared_hole_sizes``, ``problems``, ``npt_hours``...) - renamed to neither "best" nor
    "safe": they are recorded overlap counts, nothing else.
    """

    kind: str
    anchor: str | None
    anchor_name: str | None
    subjects: tuple[SubjectIdentity, ...]
    discovered: tuple[Mapping[str, Any], ...]
    profiles: tuple[Mapping[str, Any], ...]
    shared_problem_types: tuple[str, ...]
    shared_hole_sizes: tuple[str, ...]
    same_field: bool
    offset_limit: int | None
    offset_returned: int | None
    offset_at_limit: bool
    profiles_truncated: bool
    since: str | None
    until: str | None
    window_applied: bool

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "anchor": self.anchor,
            "anchor_name": self.anchor_name,
            "subjects": [subject.payload() for subject in self.subjects],
            "discovered": [dict(row) for row in self.discovered],
            "profiles": [dict(profile) for profile in self.profiles],
            "shared_problem_types": list(self.shared_problem_types),
            "shared_hole_sizes": list(self.shared_hole_sizes),
            "same_field": bool(self.same_field),
            "offset_limit": self.offset_limit,
            "offset_returned": self.offset_returned,
            "offset_at_limit": bool(self.offset_at_limit),
            "profiles_truncated": bool(self.profiles_truncated),
            "window": {
                "applied": bool(self.window_applied),
                "since": self.since,
                "until": self.until,
                "windowed_domains": list(WINDOWED_DOMAINS),
                "undated_behavior": UNDATED_BEHAVIOR,
            },
        }


@dataclass(frozen=True)
class MetricRow:
    """One metric across every subject: per-subject value with its state, plus the overall
    comparability verdict for the row.

    ``values[well_id]`` always has the same four keys (``value``, ``unit``, ``value_state``,
    ``comparability``).  The row's ``comparability`` only ever takes a value from
    :data:`COMPARABILITY_STATES`; ``INCOMPARABLE`` rows keep every source value and unit
    visible and add a machine-readable ``units`` map - nothing is converted, ever.
    """

    metric: str
    label: str
    claim_kind: str
    values: Mapping[str, Mapping[str, Any]]
    comparability: str
    units: Mapping[str, str] | None = None
    note: str = ""
    limitations: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "label": self.label,
            "claim_kind": self.claim_kind,
            "values": {key: dict(value) for key, value in self.values.items()},
            "comparability": self.comparability,
            "units": (dict(self.units) if self.units else None),
            "note": self.note,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class ComparisonSection:
    """A group of metric rows under one domain, with each subject's full source detail.

    ``detail`` maps well id -> the payload the certified method returned for that subject (or
    a faithful fold of it), so every matrix number can be traced to its source without a
    second query.  Subjects' details sit side by side and are never merged.
    """

    section: str
    label: str
    claim_kind: str
    metrics: tuple[MetricRow, ...]
    detail: Mapping[str, Any]

    def payload(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "label": self.label,
            "claim_kind": self.claim_kind,
            "metrics": [metric.payload() for metric in self.metrics],
            "detail": {key: dict(value) for key, value in self.detail.items()},
        }


@dataclass(frozen=True)
class ComparisonPack:
    """The whole deterministic comparison surface for one selection, as plain values."""

    schema: str
    request: ComparisonRequest
    basis: ComparisonBasis
    sections: tuple[ComparisonSection, ...]
    evidence: tuple[EvidenceRef, ...]
    limitations: tuple[str, ...]
    freshness: Mapping[str, str]
    observations: tuple[str, ...]
    summary: Mapping[str, Any]
    identity: str

    @property
    def well_ids(self) -> tuple[str, ...]:
        return tuple(subject.well_id for subject in self.basis.subjects)

    @property
    def subject_names(self) -> tuple[str, ...]:
        return tuple(subject.name for subject in self.basis.subjects)

    def to_dict(self) -> dict[str, Any]:
        """The stable machine-readable form.  Key order follows the dataclass fields."""
        return {
            "schema": self.schema,
            "request": self.request.payload(),
            "basis": self.basis.payload(),
            "sections": [section.payload() for section in self.sections],
            "evidence": [ref.payload() for ref in self.evidence],
            "limitations": list(self.limitations),
            "freshness": dict(self.freshness),
            "observations": list(self.observations),
            "summary": dict(self.summary),
            "identity": self.identity,
        }


# --------------------------------------------------------------------------------------
# The comparability engine
# --------------------------------------------------------------------------------------


def _value_entry(
    value: Any,
    *,
    unit: str | None = None,
    state: str = VALUE_STATED,
    comparability: str = MISSING,
) -> dict[str, Any]:
    """One subject's cell, always the same four keys."""
    return {
        "value": value,
        "unit": (unit or None),
        "value_state": state,
        "comparability": comparability,
    }


def _metric_row(
    metric: str,
    label: str,
    values: Mapping[str, dict[str, Any]],
    *,
    claim_kind: str = CLAIM_FACT,
    note: str = "",
    limitations: Sequence[str] = (),
    treat_units: bool = True,
) -> MetricRow:
    """Build a metric row and run the comparability verdict over its cells.

    The verdict, in order: ``INCOMPARABLE`` when two stated values carry different units
    (values and units stay visible; nothing is merged) - then ``UNRESOLVED`` when some
    stated value has a unit and another stated value has none (agreement is not provable
    either way) - then ``MISSING`` when fewer than two subjects stated a value (one number
    cannot compare) or none did - else ``COMPARABLE``.  Non-stated cells keep their own state
    as their comparability (``MISSING`` / ``UNASSESSED`` / ``NO_RECORDS`` / ...), so a missing
    value can never masquerade as a zero.  ``treat_units=False`` marks rows whose cells are
    non-numeric bundles (status counts, by-status maps): presence is structural and unit-free,
    so the verdict is ``COMPARABLE`` as soon as at least two subjects hold the bundle.
    """
    stated = [
        key
        for key, cell in values.items()
        if cell.get("value_state") in _VALUE_COMPARABLE_STATES and cell.get("value") is not None
    ]
    units = {
        str(values[key].get("unit")) for key in stated if treat_units and values[key].get("unit")
    }
    unitless = [key for key in stated if treat_units and not values[key].get("unit")]

    verdict = MISSING
    row_limitations: list[str] = list(limitations)
    if len(stated) >= 2:
        if treat_units and len(units) > 1:
            verdict = INCOMPARABLE
            row_limitations.append("incomparable_units")
        elif treat_units and units and unitless:
            verdict = UNRESOLVED
        else:
            verdict = COMPARABLE

    for cell in values.values():
        if key_stated(cell):
            if verdict == INCOMPARABLE:
                cell["comparability"] = INCOMPARABLE if cell.get("unit") in units else UNRESOLVED
            elif verdict == UNRESOLVED and not cell.get("unit"):
                cell["comparability"] = UNRESOLVED
            else:
                cell["comparability"] = verdict
        else:
            cell["comparability"] = str(cell.get("value_state") or MISSING)

    return MetricRow(
        metric=metric,
        label=label,
        claim_kind=claim_kind,
        values={key: dict(value) for key, value in values.items()},
        comparability=verdict,
        units=(
            {key: str(value.get("unit")) for key, value in values.items() if value.get("unit")}
            if verdict == INCOMPARABLE
            else None
        ),
        note=note,
        limitations=tuple(dict.fromkeys(row_limitations)),
    )


def key_stated(cell: Mapping[str, Any]) -> bool:
    """Is this cell a real number a comparison may use?"""
    return cell.get("value_state") in _VALUE_COMPARABLE_STATES and cell.get("value") is not None


def _worst_freshness(readings: Sequence[str]) -> str:
    """The least-fresh reading across subjects, by :data:`_FRESHNESS_RANK`."""
    if not readings:
        return FRESH_NOT_AVAILABLE
    return max(readings, key=lambda state: _FRESHNESS_RANK.get(state, 2))


# --------------------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------------------


@dataclass
class _SubjectData:
    """Everything one column of the matrix reads, already folded by certified methods."""

    identity: SubjectIdentity
    sections: dict[str, Any] = dataclass_field(default_factory=dict)
    evidence: list[EvidenceRef] = dataclass_field(default_factory=list)
    conflicts: list[dict[str, Any]] = dataclass_field(default_factory=list)
    scales: dict[str, int] = dataclass_field(default_factory=dict)
    hole_sizes: tuple[str, ...] = ()
    freshness: dict[str, str] = dataclass_field(default_factory=dict)


class ComparisonIntelligence(FieldIntelligence):
    """Builds :class:`ComparisonPack` values for an explicit or discovered selection.

    Read-side only: nothing is persisted, no session or ORM object appears in any returned
    contract, and the only public entry point is :meth:`compare`.
    """

    # -- selection ----------------------------------------------------------------------

    @staticmethod
    def _validate_well_ids(well_ids: Sequence[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in well_ids:
            value = str(raw or "").strip()
            if not value:
                raise ValidationError(
                    "a comparison subject id cannot be empty",
                    hint="drop the empty --well/--offsets value",
                )
            cleaned.append(value)
        if len(cleaned) < 2:
            raise ValidationError(
                "a comparison needs at least two wells",
                hint="pass --well twice, or --anchor with --offsets / discovered candidates",
            )
        duplicates = sorted({value for value in cleaned if cleaned.count(value) > 1})
        if duplicates:
            raise ValidationError(
                f"the same well is named more than once: {', '.join(duplicates)}",
                hint="list each subject exactly once",
            )
        return cleaned

    def _well_rows(self, well_ids: Sequence[str]) -> dict[str, Well]:
        rows = {
            str(row.id): row
            for row in self.session.execute(
                select(Well).where(Well.id.in_(list(well_ids)))
            ).scalars()
        }
        missing = sorted({value for value in well_ids if value not in rows})
        if missing:
            raise ValidationError(
                f"unknown well: {', '.join(missing)}",
                hint="the ids must exist in this workspace",
            )
        return rows

    def _resolve_selection(
        self,
        *,
        well_ids: Sequence[str],
        anchor: str,
        offsets: Sequence[str],
        offset_limit: int,
    ) -> tuple[str, str, list[str], list[dict[str, Any]], bool]:
        """Validate the request and return ``(kind, anchor_id, ordered_ids, discovered,
        offsets_were_discovered)``.

        Scope is never broadened: zero or one well is an error, mixing ``--well`` with
        ``--anchor/--offsets`` is an error, duplicates are an error, unknown ids are an
        error.  ``--anchor X`` with no ``--offsets`` discovers candidates through the
        existing ``offset_candidates`` method, and that discovery *is* the selection basis
        (``kind`` becomes ``offset_candidates``).  Explicitly named offsets keep
        ``explicit_wells`` with the anchor recorded alongside them.
        """
        anchor_value = str(anchor or "").strip()
        named_offsets = [str(value or "").strip() for value in offsets]
        named_offsets = [value for value in named_offsets if value]
        has_explicit = any(str(value or "").strip() for value in well_ids)
        if has_explicit and (anchor_value or named_offsets):
            raise ValidationError(
                "contradictory scope: --well cannot be combined with --anchor/--offsets",
                hint="choose one selection style",
            )
        if anchor_value:
            if named_offsets:
                ordered = self._validate_well_ids([anchor_value, *named_offsets])
                return "explicit_wells", anchor_value, ordered, [], False
            candidates = self.offset_candidates(
                anchor_value,
                same_field_only=True,
                limit=max(int(offset_limit), 1),
            )
            if not candidates:
                raise ValidationError(
                    f"no offset candidates recorded for anchor {anchor_value}",
                    hint="pass --offsets explicitly, or use --well",
                )
            discovered = [dict(row) for row in candidates]
            ordered = self._validate_well_ids(
                [anchor_value, *[str(row["well_id"]) for row in candidates]]
            )
            return "offset_candidates", anchor_value, ordered, discovered, True
        cleaned = self._validate_well_ids(well_ids)
        return "explicit_wells", "", cleaned, [], False

    # -- per-subject reads --------------------------------------------------------------

    def _subject_identity(self, row: Well, *, selection: str) -> SubjectIdentity:
        return SubjectIdentity(
            well_id=str(row.id),
            name=str(row.name),
            selection=selection,
            field_id=(str(row.field_id) if row.field_id else None),
            project_id=(str(row.project_id) if row.project_id else None),
            spud_date=_iso(row.spud_date),
        )

    def _load_subject(
        self,
        identity: SubjectIdentity,
        *,
        decision: DecisionIntelligence,
        since: Any,
        until: Any,
        detail: int,
        evidence_limit: int,
    ) -> _SubjectData:
        """Read one subject through the certified decision sections - the same folds a
        well-scoped DecisionPack runs, scoped to exactly this well - plus the two facts only
        comparison needs (risk scale identity, open conflicts)."""
        well_id = identity.well_id
        subject = _SubjectData(identity=identity)

        execution, execution_ids = decision._execution(
            scope_key="well_id", scope_value=well_id, detail=max(int(detail), 1)
        )
        operations = decision._operations(
            scope_key="well_id", scope_value=well_id, since=since, until=until
        )
        economics = decision._economics(
            scope_key="well_id", scope_value=well_id, detail=max(int(detail), 1)
        )
        risk, risk_evidence = decision._risk(
            scope_key="well_id",
            scope_value=well_id,
            wells=[well_id],
            evidence_limit=int(evidence_limit),
        )
        learning, recommendations, learning_evidence = decision._learning(
            scope_key="well_id",
            scope_value=well_id,
            wells=[well_id],
            evidence_limit=int(evidence_limit),
        )
        patterns, pattern_evidence = decision._patterns(
            scope_key="well_id",
            scope_value=well_id,
            subject=DecisionSubject(
                kind="well",
                id=well_id,
                name=identity.name,
                field_id=identity.field_id,
                project_id=identity.project_id,
            ),
            evidence_limit=int(evidence_limit),
        )
        calculations, calculation_evidence = decision._calculations(
            scope_key="well_id",
            scope_value=well_id,
            wells=[well_id],
            evidence_limit=int(evidence_limit),
        )
        cost_evidence = decision._cost_evidence(
            scope_key="well_id", scope_value=well_id, evidence_limit=int(evidence_limit)
        )
        execution_evidence = decision._execution_evidence(
            execution, execution_ids, int(evidence_limit)
        )

        subject.sections = {
            "execution": execution,
            "operations": operations,
            "economics": economics,
            "risk": risk,
            "learning": learning,
            "recommendations": recommendations,
            "patterns": patterns,
            "calculations": calculations,
        }
        subject.evidence = [
            *execution_evidence,
            *risk_evidence,
            *learning_evidence,
            *pattern_evidence,
            *calculation_evidence,
            *cost_evidence,
        ]

        # Risk scale identity: RiskSection does not carry it, and severity comparability is
        # refused without it.  Same membership and same current rule as the risk fold
        # (status != SUPERSEDED), so the scales and the counts can never disagree.
        membership = decision._membership(
            RiskRecord, scope_key="well_id", scope_value=well_id, wells=[well_id]
        )
        scale_rows = list(
            self.session.execute(
                select(RiskRecord.scale, func.count(RiskRecord.id))
                .where(membership, RiskRecord.status != str(RiskLifecycle.SUPERSEDED))
                .group_by(RiskRecord.scale)
            )
        )
        subject.scales = {
            str(scale or ""): int(count)
            for scale, count in sorted(scale_rows, key=lambda row: str(row[0] or ""))
        }

        # Open knowledge conflicts for this subject: preserved, never resolved here.
        for conflict in KnowledgeRepository(self.session).conflicts(well_id=well_id, limit=100):
            subject.conflicts.append(
                {
                    "id": str(conflict.id),
                    "lookup_key": str(conflict.lookup_key),
                    "status": str(conflict.status),
                    "well_id": (str(conflict.well_id) if conflict.well_id else None),
                }
            )

        subject.freshness = dict(
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
        return subject

    def _hole_sizes(self, well_ids: Sequence[str]) -> dict[str, tuple[str, ...]]:
        """Distinct recorded hole sizes per subject, for the shared-basis statement.

        The source is ``ProblemOccurrence.hole_size_in`` - the very column
        ``offset_candidates`` counts shared hole sizes from - so the shared-basis statement
        of an explicit selection and the discovery method can never speak of different
        things under one name."""
        rows = list(
            self.session.execute(
                select(ProblemOccurrence.well_id, ProblemOccurrence.hole_size_in)
                .where(
                    ProblemOccurrence.well_id.in_(list(well_ids)),
                    ProblemOccurrence.hole_size_in.is_not(None),
                )
                .distinct()
            )
        )
        grouped: dict[str, list[str]] = {str(wid): [] for wid in well_ids}
        for wid, size in rows:
            grouped.setdefault(str(wid), []).append(str(size))
        return {wid: tuple(sorted(values)) for wid, values in grouped.items()}

    def _well_samples(
        self,
        model: Any,
        record_type: str,
        date_column: Any,
        *,
        well_ids: Sequence[str],
        since: Any,
        until: Any,
        limit: int,
    ) -> dict[str, list[str]]:
        """Bounded ``structured:<type>:<id>`` samples per subject, one query for all of them.

        The inner window applies the same date predicates the certified aggregate applies, so
        a sampled row can never sit outside the window its count was taken under.
        """
        if not well_ids or int(limit) <= 0:
            return {}
        from ..search.structured import structured_record_id

        ranked = select(
            model.id,
            model.well_id,
            func.row_number().over(partition_by=model.well_id, order_by=model.id).label("rn"),
        ).where(model.well_id.in_(list(well_ids)))
        if date_column is not None:
            ranked = ranked.where(*self._window(date_column, since, until))
        sub = ranked.subquery()
        rows = list(
            self.session.execute(
                select(sub.c.well_id, sub.c.id)
                .where(sub.c.rn <= int(limit))
                .order_by(sub.c.well_id, sub.c.id)
            )
        )
        samples: dict[str, list[str]] = {}
        for wid, row_id in rows:
            samples.setdefault(str(wid), []).append(structured_record_id(record_type, str(row_id)))
        return samples

    # -- section builders ----------------------------------------------------------------

    @staticmethod
    def _values(
        subjects: Sequence[_SubjectData],
        build: Any,
    ) -> dict[str, dict[str, Any]]:
        """Run ``build(subject) -> cell`` for every subject, preserving subject order."""
        return {subject.identity.well_id: build(subject) for subject in subjects}

    def _execution_section(self, subjects: Sequence[_SubjectData]) -> ComparisonSection:
        statuses = sorted(
            {key for subject in subjects for key in subject.sections["execution"].by_status}
        )
        matched = sorted(
            {key for subject in subjects for key in subject.sections["execution"].by_matched_by}
        )
        metrics: list[MetricRow] = [
            _metric_row(
                "plan.sections",
                "Sections in plan scope",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(subject.sections["execution"].sections),
                        state=VALUE_COUNTED,
                    ),
                ),
            ),
            _metric_row(
                "plan.rows",
                "Plan/actual metric rows",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(subject.sections["execution"].rows),
                        state=VALUE_COUNTED,
                    ),
                ),
                claim_kind=CLAIM_DERIVED,
            ),
        ]
        for status in statuses:
            metrics.append(
                _metric_row(
                    f"plan.status.{status.lower()}",
                    f"Rows with status {status}",
                    self._values(
                        subjects,
                        lambda subject, key=status: _value_entry(
                            _as_int(subject.sections["execution"].by_status.get(key, 0)),
                            state=VALUE_COUNTED,
                        ),
                    ),
                    claim_kind=CLAIM_DERIVED,
                )
            )
        for key in matched:
            metrics.append(
                _metric_row(
                    f"plan.matched.{key.lower()}",
                    f"Sections matched by {key}",
                    self._values(
                        subjects,
                        lambda subject, name=key: _value_entry(
                            _as_int(subject.sections["execution"].by_matched_by.get(name, 0)),
                            state=VALUE_COUNTED,
                        ),
                    ),
                    claim_kind=CLAIM_DERIVED,
                )
            )
        return ComparisonSection(
            section="execution",
            label="Plan versus actual",
            claim_kind=CLAIM_DERIVED,
            metrics=tuple(metrics),
            detail={
                subject.identity.well_id: subject.sections["execution"].payload()
                for subject in subjects
            },
        )

    def _operations_section(
        self, subjects: Sequence[_SubjectData], *, since: Any = None, until: Any = None
    ) -> ComparisonSection:
        def npt(subject: _SubjectData) -> Mapping[str, Any]:
            return subject.sections["operations"].npt

        def problems(subject: _SubjectData) -> Mapping[str, Any]:
            return subject.sections["operations"].problems

        def wc(subject: _SubjectData) -> Mapping[str, Any]:
            return subject.sections["operations"].well_control

        def hse(subject: _SubjectData) -> Mapping[str, Any]:
            return subject.sections["operations"].hse

        def npt_hours_cell(subject: _SubjectData) -> dict[str, Any]:
            data = npt(subject)
            rows = _as_int(data.get("rows"))
            unknown = _as_int(data.get("unknown_duration"))
            if rows <= 0:
                # No NPT rows at all: "no records" is not "0.0 hours recorded".
                return _value_entry(None, unit="h", state=VALUE_NO_RECORDS)
            timed = rows - unknown
            if timed <= 0:
                # Rows exist but none states a duration: recorded, unvalued - never zero.
                return _value_entry(None, unit="h", state=VALUE_UNASSESSED)
            total = round(_as_float(data.get("total_hours")), 4)
            if unknown > 0:
                return _value_entry(total, unit="h", state=VALUE_PARTIAL)
            return _value_entry(total, unit="h", state=VALUE_STATED)

        metrics: list[MetricRow] = [
            _metric_row(
                "npt.rows",
                "NPT rows in scope",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(npt(subject).get("rows")), state=VALUE_COUNTED
                    ),
                ),
            ),
            _metric_row(
                "npt.hours",
                "NPT hours recorded",
                self._values(subjects, npt_hours_cell),
                note="sum of stated durations; rows without a duration are counted, never zero-filled",
            ),
            _metric_row(
                "npt.rows_without_duration",
                "NPT rows without a stated duration",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(npt(subject).get("unknown_duration")), state=VALUE_COUNTED
                    ),
                ),
            ),
            _metric_row(
                "problems.occurrences",
                "Problem occurrences in scope",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(problems(subject).get("occurrences")), state=VALUE_COUNTED
                    ),
                ),
            ),
            _metric_row(
                "problems.types",
                "Distinct recorded problem types",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        len(problems(subject).get("by_type") or {}), state=VALUE_COUNTED
                    ),
                ),
            ),
            _metric_row(
                "well_control.events",
                "Well-control rows in scope",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(wc(subject).get("events")), state=VALUE_COUNTED
                    ),
                ),
            ),
            _metric_row(
                "well_control.with_explicit_cause",
                "Well-control rows with an explicit source-stated cause",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(wc(subject).get("with_explicit_cause")), state=VALUE_COUNTED
                    ),
                ),
            ),
            _metric_row(
                "hse.incidents",
                "HSE incidents attached to the well",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(hse(subject).get("incidents")), state=VALUE_COUNTED
                    ),
                ),
                note="site-scoped rows are never attached to a well subject",
            ),
        ]
        problem_types = sorted(
            {key for subject in subjects for key in (problems(subject).get("by_type") or {})}
        )
        for ptype in problem_types:
            metrics.append(
                _metric_row(
                    f"problems.occurrences.{ptype}",
                    f"Problem occurrences of type {ptype}",
                    self._values(
                        subjects,
                        lambda subject, key=ptype: _value_entry(
                            _as_int(
                                (problems(subject).get("by_type") or {})
                                .get(key, {})
                                .get("occurrences", 0)
                            ),
                            state=VALUE_COUNTED,
                        ),
                    ),
                )
            )
        event_types = sorted(
            {key for subject in subjects for key in (wc(subject).get("by_event_type") or {})}
        )
        for etype in event_types:
            metrics.append(
                _metric_row(
                    f"well_control.events.{etype}",
                    f"Well-control rows of event type {etype}",
                    self._values(
                        subjects,
                        lambda subject, key=etype: _value_entry(
                            _as_int((wc(subject).get("by_event_type") or {}).get(key, 0)),
                            state=VALUE_COUNTED,
                        ),
                    ),
                )
            )
        incident_types = sorted(
            {key for subject in subjects for key in (hse(subject).get("by_incident_type") or {})}
        )
        for itype in incident_types:
            metrics.append(
                _metric_row(
                    f"hse.incidents.{itype}",
                    f"HSE incidents of type {itype}",
                    self._values(
                        subjects,
                        lambda subject, key=itype: _value_entry(
                            _as_int((hse(subject).get("by_incident_type") or {}).get(key, 0)),
                            state=VALUE_COUNTED,
                        ),
                    ),
                )
            )

        detail: dict[str, Any] = {}
        for subject in subjects:
            payload = dict(subject.sections["operations"].payload())
            # The certified section fold does not echo the window (it carries the numbers,
            # not the request), so the window facts come from the request that produced them.
            payload["window"] = {
                "applied": bool(_iso(since) or _iso(until)),
                "since": _iso(since),
                "until": _iso(until),
                "windowed_domains": list(WINDOWED_DOMAINS),
                "undated_behavior": UNDATED_BEHAVIOR,
            }
            detail[subject.identity.well_id] = payload
        return ComparisonSection(
            section="operations",
            label="NPT, problems, well control and HSE",
            claim_kind=CLAIM_FACT,
            metrics=tuple(metrics),
            detail=detail,
        )

    def _economics_section(self, subjects: Sequence[_SubjectData]) -> ComparisonSection:
        def summary(subject: _SubjectData) -> Mapping[str, Any]:
            return subject.sections["economics"].summary

        def by_currency(subject: _SubjectData) -> dict[str, Any]:
            return dict(summary(subject).get("by_currency") or {})

        currencies = sorted({code for subject in subjects for code in by_currency(subject)})
        metrics: list[MetricRow] = []

        def side_cell(subject: _SubjectData, side: str) -> dict[str, Any]:
            """One subject's single-side total: its total in *its* currency, only when
            exactly one currency states that side.  No lines on that side -> MISSING (never
            0.0); two currencies -> no single total exists, so the value stays null and the
            per-currency rows carry the real numbers."""
            rows = by_currency(subject)
            codes = sorted(
                code
                for code, bucket in rows.items()
                if _as_int((bucket or {}).get(f"{side}_lines", 0)) > 0
            )
            if not codes:
                return _value_entry(None, state=MISSING)
            if len(codes) > 1:
                return _value_entry(None, state=VALUE_UNASSESSED)
            code = codes[0]
            return _value_entry(
                round(_as_float((rows[code] or {}).get(side)), 4),
                unit=code,
                state=VALUE_STATED,
            )

        for side in ("planned", "actual"):
            metrics.append(
                _metric_row(
                    f"cost.{side}",
                    f"Total {side} cost as stated (one currency)",
                    self._values(
                        subjects,
                        lambda subject, name=side: side_cell(subject, name),
                    ),
                    claim_kind=CLAIM_DERIVED,
                    note="unit is the source currency; currencies are never converted or summed, so differing currencies make this row INCOMPARABLE",
                )
            )
        for code in currencies:
            metrics.append(
                _metric_row(
                    f"cost.planned.{code}",
                    f"Planned cost stated in {code}",
                    self._values(
                        subjects,
                        lambda subject, cur=code: _planned_cell(by_currency(subject), cur),
                    ),
                    claim_kind=CLAIM_DERIVED,
                    note="per-currency totals only; never summed or converted across currencies",
                )
            )
            metrics.append(
                _metric_row(
                    f"cost.actual.{code}",
                    f"Actual cost stated in {code}",
                    self._values(
                        subjects,
                        lambda subject, cur=code: _actual_cell(by_currency(subject), cur),
                    ),
                    claim_kind=CLAIM_DERIVED,
                    note="per-currency totals only; never summed or converted across currencies",
                )
            )
        return ComparisonSection(
            section="economics",
            label="Costs by currency",
            claim_kind=CLAIM_DERIVED,
            metrics=tuple(metrics),
            detail={
                subject.identity.well_id: subject.sections["economics"].payload()
                for subject in subjects
            },
        )

    def _risk_section(self, subjects: Sequence[_SubjectData]) -> ComparisonSection:
        def risk(subject: _SubjectData) -> RiskSection:
            return subject.sections["risk"]

        metrics: list[MetricRow] = [
            _metric_row(
                "risk.current",
                "Current risks on record",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(risk(subject).current), state=VALUE_COUNTED
                    ),
                ),
            ),
            _metric_row(
                "risk.open",
                "Risks with status OPEN",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(risk(subject).by_status.get(str(RiskLifecycle.OPEN), 0)),
                        state=VALUE_COUNTED,
                    ),
                ),
            ),
            _metric_row(
                "risk.severity_unassessed",
                "Current risks with no source-stated severity",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(risk(subject).stated.get("severity_unassessed", 0)),
                        state=VALUE_COUNTED,
                    ),
                ),
                claim_kind=CLAIM_DERIVED,
            ),
            _metric_row(
                "risk.without_evidence",
                "Risk rows with no provenance entry",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(risk(subject).without_evidence), state=VALUE_COUNTED
                    ),
                ),
            ),
        ]
        # Severity bands: comparable only while the recorded scale identities match.  Counts
        # are never hidden - only the comparability verdict changes.
        bands = sorted({band for subject in subjects for band in risk(subject).severity_bands})
        scale_note, scale_state, scale_limitation = self._scale_gate(subjects)
        for band in bands:
            row = _metric_row(
                f"risk.severity.{band}",
                f"Current risks in severity band {band}",
                self._values(
                    subjects,
                    lambda subject, key=band: _value_entry(
                        _as_int(risk(subject).severity_bands.get(key, 0)),
                        state=VALUE_COUNTED,
                    ),
                ),
                claim_kind=CLAIM_DERIVED,
                note=scale_note,
            )
            if scale_state in (INCOMPARABLE, UNRESOLVED):
                values = {key: dict(cell) for key, cell in row.values.items()}
                for cell in values.values():
                    if cell["value"] is not None:
                        cell["comparability"] = scale_state
                row = MetricRow(
                    metric=row.metric,
                    label=row.label,
                    claim_kind=row.claim_kind,
                    values=values,
                    comparability=scale_state,
                    units=None,
                    note=scale_note,
                    limitations=(scale_limitation,) if scale_limitation else (),
                )
            metrics.append(row)
        return ComparisonSection(
            section="risk",
            label="Risks as recorded",
            claim_kind=CLAIM_FACT,
            metrics=tuple(metrics),
            detail={
                subject.identity.well_id: {
                    **subject.sections["risk"].payload(),
                    "scales": dict(subject.scales),
                }
                for subject in subjects
            },
        )

    def _scale_gate(self, subjects: Sequence[_SubjectData]) -> tuple[str, str, str]:
        """Whether severity bands may be called comparable at all.

        The recorded ``RiskRecord.scale`` identity must be the same single scale on every
        subject that has current risks.  Differing known scales -> ``INCOMPARABLE``; a
        subject with risks but no recorded identity, or with no risks at all while others
        have them -> ``UNRESOLVED`` (agreement is not provable); nobody has risks -> bands
        rows will be all-``NO_RECORDS`` anyway, so ``COMPARABLE`` is harmless.
        """
        present = [dict(subject.scales) for subject in subjects if subject.scales]
        identities = {tuple(sorted(entry)) for entry in present}
        if not present:
            return "no current risk rows carry a scale on any subject", COMPARABLE, ""
        if len(present) < len(subjects) or any(len(entry) != 1 for entry in present):
            return (
                "risk scale identity is not recorded on every subject; severity bands are not "
                "provably on one scale",
                UNRESOLVED,
                "unassessed_risk",
            )
        if len(identities) > 1:
            names = sorted({name for entry in present for name in entry})
            return (
                f"risk scales differ between subjects ({', '.join(names)}); severity bands are "
                "never merged across scales",
                INCOMPARABLE,
                "incomparable_units",
            )
        name = next(iter(next(iter(identities))))
        return f"shared recorded risk scale {name}", COMPARABLE, ""

    def _learning_section(self, subjects: Sequence[_SubjectData]) -> ComparisonSection:
        def learning(subject: _SubjectData) -> LearningSection:
            return subject.sections["learning"]

        metrics = [
            _metric_row(
                "lessons.current",
                "Lessons on record (current revisions)",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(learning(subject).lessons.get("current")), state=VALUE_COUNTED
                    ),
                ),
            ),
            _metric_row(
                "lessons.approved",
                "Approved lessons",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(learning(subject).lessons.get("approved")),
                        state=VALUE_COUNTED,
                    ),
                ),
            ),
            _metric_row(
                "practices.current",
                "Practices on record (current revisions)",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(learning(subject).practices.get("current")),
                        state=VALUE_COUNTED,
                    ),
                ),
            ),
            _metric_row(
                "practices.adopted",
                "Adopted practices",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(learning(subject).practices.get("adopted")),
                        state=VALUE_COUNTED,
                    ),
                ),
            ),
        ]
        return ComparisonSection(
            section="learning",
            label="Lessons and practices",
            claim_kind=CLAIM_FACT,
            metrics=tuple(metrics),
            detail={
                subject.identity.well_id: subject.sections["learning"].payload()
                for subject in subjects
            },
        )

    def _recommendations_section(self, subjects: Sequence[_SubjectData]) -> ComparisonSection:
        def recs(subject: _SubjectData) -> RecommendationSection:
            return subject.sections["recommendations"]

        metrics = [
            _metric_row(
                "recommendations.total",
                "Recommendations on record",
                self._values(
                    subjects,
                    lambda subject: _value_entry(_as_int(recs(subject).total), state=VALUE_COUNTED),
                ),
            ),
            _metric_row(
                "recommendations.proposed",
                "Proposed recommendations",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(
                            recs(subject).by_status.get(str(RecommendationLifecycle.PROPOSED), 0)
                        ),
                        state=VALUE_COUNTED,
                    ),
                ),
            ),
            _metric_row(
                "recommendations.accepted",
                "Accepted recommendations",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(
                            recs(subject).by_status.get(str(RecommendationLifecycle.ACCEPTED), 0)
                        ),
                        state=VALUE_COUNTED,
                    ),
                ),
            ),
            _metric_row(
                "recommendations.with_evidence",
                "Recommendations carrying an explicit evidence entry",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(recs(subject).with_evidence), state=VALUE_COUNTED
                    ),
                ),
            ),
        ]
        return ComparisonSection(
            section="recommendations",
            label="Recommendations (proposals, not facts)",
            claim_kind=CLAIM_FACT,
            metrics=tuple(metrics),
            detail={
                subject.identity.well_id: subject.sections["recommendations"].payload()
                for subject in subjects
            },
        )

    def _patterns_section(self, subjects: Sequence[_SubjectData]) -> ComparisonSection:
        def patterns(subject: _SubjectData) -> PatternSection:
            return subject.sections["patterns"]

        metrics = [
            _metric_row(
                "patterns.total",
                "Observed field patterns in the subject's field scope",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(patterns(subject).total), state=VALUE_COUNTED
                    ),
                ),
            ),
            _metric_row(
                "patterns.confirmed",
                "Confirmed patterns",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(
                            (patterns(subject).by_status.get("CONFIRMED") or {}).get("count", 0)
                        ),
                        state=VALUE_COUNTED,
                    ),
                ),
            ),
            _metric_row(
                "patterns.stale",
                "Patterns marked stale",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(patterns(subject).stale), state=VALUE_COUNTED
                    ),
                ),
            ),
        ]
        return ComparisonSection(
            section="patterns",
            label="Observed patterns (descriptive)",
            claim_kind=CLAIM_FACT,
            metrics=tuple(metrics),
            detail={
                subject.identity.well_id: subject.sections["patterns"].payload()
                for subject in subjects
            },
        )

    def _calculations_section(self, subjects: Sequence[_SubjectData]) -> ComparisonSection:
        def calcs(subject: _SubjectData) -> CalculationSection:
            return subject.sections["calculations"]

        metrics = [
            _metric_row(
                "calculations.total",
                "Stored calculations in scope (never executed here)",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(calcs(subject).total), state=VALUE_COUNTED
                    ),
                ),
            ),
            _metric_row(
                "calculations.current",
                "Calculations the revision chain marks current",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(calcs(subject).current), state=VALUE_COUNTED
                    ),
                ),
            ),
            _metric_row(
                "calculations.history",
                "Calculations the revision chain marks history",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(calcs(subject).history), state=VALUE_COUNTED
                    ),
                ),
            ),
            _metric_row(
                "calculations.inputs",
                "Calculation input references in scope",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(calcs(subject).inputs), state=VALUE_COUNTED
                    ),
                ),
            ),
            _metric_row(
                "calculations.dependency_stale",
                "Inputs whose dependency is stale",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(calcs(subject).dependency.get("STALE", 0)),
                        state=VALUE_COUNTED,
                    ),
                ),
            ),
            _metric_row(
                "calculations.dependency_unresolved",
                "Inputs whose dependency is unresolved",
                self._values(
                    subjects,
                    lambda subject: _value_entry(
                        _as_int(calcs(subject).dependency.get("UNRESOLVED", 0)),
                        state=VALUE_COUNTED,
                    ),
                ),
            ),
        ]
        return ComparisonSection(
            section="calculations",
            label="Stored calculations",
            claim_kind=CLAIM_FACT,
            metrics=tuple(metrics),
            detail={
                subject.identity.well_id: subject.sections["calculations"].payload()
                for subject in subjects
            },
        )

    def _conflicts_section(self, subjects: Sequence[_SubjectData]) -> ComparisonSection:
        metrics = [
            _metric_row(
                "conflicts.open",
                "Open knowledge conflicts on this subject",
                self._values(
                    subjects,
                    lambda subject: _value_entry(len(subject.conflicts), state=VALUE_COUNTED),
                ),
                note="conflicts are preserved as-is; this layer never resolves them",
            ),
            _metric_row(
                "profile.spud_date",
                "Spud date as recorded",
                self._values(
                    subjects,
                    lambda subject: _value_entry(subject.identity.spud_date, state=VALUE_STATED),
                ),
            ),
        ]
        return ComparisonSection(
            section="conflicts",
            label="Open knowledge conflicts and profile",
            claim_kind=CLAIM_FACT,
            metrics=tuple(metrics),
            detail={
                subject.identity.well_id: {"conflicts": list(subject.conflicts)}
                for subject in subjects
            },
        )

    # -- evidence --------------------------------------------------------------------------

    def _operations_evidence(
        self,
        subjects: Sequence[_SubjectData],
        *,
        since: Any,
        until: Any,
        evidence_limit: int,
    ) -> list[EvidenceRef]:
        """Aggregate method+scope+window refs (re-executable, like the decision pack) plus
        bounded structured row-id samples for the four windowed operational domains."""
        refs: list[EvidenceRef] = []
        windowed_scope_base = {"since": _iso(since), "until": _iso(until)}
        for subject in subjects:
            well_id = subject.identity.well_id
            operations = subject.sections["operations"].payload()
            scope = {
                **_scope_payload(well_id=well_id),
                **windowed_scope_base,
            }
            for domain, method, count in (
                ("npt_record", "FieldIntelligence.npt", operations.get("npt", {}).get("rows", 0)),
                (
                    "problem_occurrence",
                    "FieldIntelligence.problems",
                    operations.get("problems", {}).get("occurrences", 0),
                ),
                (
                    "well_control_event",
                    "FieldIntelligence.well_control",
                    operations.get("well_control", {}).get("events", 0),
                ),
                (
                    "hse_incident",
                    "FieldIntelligence.hse",
                    operations.get("hse", {}).get("incidents", 0),
                ),
            ):
                if int(count):
                    refs.append(
                        EvidenceRef(
                            section="operations",
                            domain=domain,
                            kind="aggregate",
                            identity="method",
                            count=int(count),
                            method=method,
                            scope=dict(scope),
                        )
                    )
        sample_specs = (
            (NptRecord, "npt_record", NptRecord.started_at, "npt_record"),
            (
                ProblemOccurrence,
                "problem_occurrence",
                ProblemOccurrence.occurred_at,
                "problem_occurrence",
            ),
            (
                WellControlEvent,
                "well_control_event",
                WellControlEvent.occurred_at,
                "well_control_event",
            ),
            (HseIncident, "hse_incident", HseIncident.occurred_at, "hse_incident"),
        )
        well_ids = [subject.identity.well_id for subject in subjects]
        for model, domain, date_column, record_type in sample_specs:
            samples = self._well_samples(
                model,
                record_type,
                date_column,
                well_ids=well_ids,
                since=since,
                until=until,
                limit=int(evidence_limit),
            )
            counts = {
                subject.identity.well_id: _domain_count(subject, domain) for subject in subjects
            }
            for subject in subjects:
                well_id = subject.identity.well_id
                ids = samples.get(well_id) or []
                total = int(counts.get(well_id, 0))
                if not total:
                    continue
                refs.append(
                    EvidenceRef(
                        section="operations",
                        domain=domain,
                        kind="rows",
                        identity="structured",
                        count=total,
                        sample=tuple(ids),
                        truncated=total > len(ids),
                        scope={
                            **_scope_payload(well_id=well_id),
                            **windowed_scope_base,
                        },
                    )
                )
        return refs

    def _conflict_evidence(
        self, subjects: Sequence[_SubjectData], *, evidence_limit: int
    ) -> list[EvidenceRef]:
        refs: list[EvidenceRef] = []
        for subject in subjects:
            rows = subject.conflicts
            if not rows:
                continue
            ids = [str(row["id"]) for row in rows[: int(evidence_limit)]]
            refs.append(
                EvidenceRef(
                    section="conflicts",
                    domain="knowledge_conflict",
                    kind="rows",
                    identity="record",
                    count=len(rows),
                    sample=tuple(ids),
                    truncated=len(rows) > len(ids),
                    scope=_scope_payload(well_id=subject.identity.well_id),
                )
            )
        return refs

    # -- pack assembly ----------------------------------------------------------------------

    def compare(
        self,
        *,
        well_ids: Sequence[str] = (),
        anchor: str = "",
        offsets: Sequence[str] = (),
        since: Any = None,
        until: Any = None,
        detail: int = 1,
        evidence_limit: int = 10,
        offset_limit: int = 10,
    ) -> ComparisonPack:
        """Build the deterministic comparison pack for exactly the wells selected.

        The pack is re-runnable: same request, same repository state, same content identity.
        Nothing is persisted and no aggregate is cached - staleness is exactly what the
        freshness and limitation fields exist to show.
        """
        offset_limit = max(int(offset_limit), 1)
        evidence_limit = max(int(evidence_limit), 0)
        detail_value = max(int(detail), 0)
        kind, anchor_id, ordered, discovered, discovered_flag = self._resolve_selection(
            well_ids=well_ids,
            anchor=anchor,
            offsets=offsets,
            offset_limit=offset_limit,
        )
        rows = self._well_rows(ordered)

        # Subject order is the request's order (or the discovery method's own deterministic
        # order) - never re-sorted, never ranked.
        selections: dict[str, str] = {}
        if kind == "offset_candidates":
            selections[anchor_id] = "anchor"
            for wid in ordered:
                selections.setdefault(wid, "discovered_offset")
        else:
            for wid in (str(value).strip() for value in well_ids):
                if wid:
                    selections[wid] = "explicit"
            if anchor_id:
                selections[anchor_id] = "anchor"
                for wid in (str(value).strip() for value in offsets):
                    if wid:
                        selections[wid] = "named_offset"
        identities = [
            self._subject_identity(rows[wid], selection=selections.get(wid, "explicit"))
            for wid in ordered
        ]

        decision = DecisionIntelligence(self.session)
        subjects = [
            self._load_subject(
                identity,
                decision=decision,
                since=since,
                until=until,
                detail=detail_value,
                evidence_limit=evidence_limit,
            )
            for identity in identities
        ]
        holes = self._hole_sizes(ordered)

        # Shared recorded basis: intersections, stated as facts.
        type_sets = [
            set(subject.sections["operations"].problems.get("by_type") or {})
            for subject in subjects
        ]
        shared_types = tuple(sorted(set.intersection(*type_sets))) if type_sets else ()
        hole_sets = [set(holes[wid]) for wid in ordered]
        shared_holes = tuple(sorted(set.intersection(*hole_sets))) if hole_sets else ()
        field_ids = {identity.field_id for identity in identities}

        # -- sections ---------------------------------------------------------------------
        sections = (
            self._execution_section(subjects),
            self._operations_section(subjects, since=since, until=until),
            self._economics_section(subjects),
            self._risk_section(subjects),
            self._learning_section(subjects),
            self._recommendations_section(subjects),
            self._patterns_section(subjects),
            self._calculations_section(subjects),
            self._conflicts_section(subjects),
        )

        # -- offset candidate profiles (bounded) ------------------------------------------
        profiles: list[Mapping[str, Any]] = []
        profiles_truncated = False
        if discovered_flag:
            metric_rows = [row for section in sections for row in section.metrics]
            profile_candidates = [
                (
                    candidate,
                    next(s for s in subjects if s.identity.well_id == str(candidate["well_id"])),
                )
                for candidate in discovered
            ]
            if len(profile_candidates) > OFFSET_PROFILE_CAP:
                profile_candidates = profile_candidates[:OFFSET_PROFILE_CAP]
                profiles_truncated = True
            anchor_subject = next((s for s in subjects if s.identity.well_id == anchor_id), None)
            anchor_types = (
                set(anchor_subject.sections["operations"].problems.get("by_type") or {})
                if anchor_subject
                else set()
            )
            for candidate, subject in profile_candidates:
                candidate_id = str(candidate["well_id"])
                comparable = sorted(
                    row.metric for row in metric_rows if row.comparability == COMPARABLE
                )
                incomparable = sorted(
                    row.metric for row in metric_rows if row.comparability == INCOMPARABLE
                )
                unresolved = sorted(
                    row.metric
                    for row in metric_rows
                    if row.comparability == UNRESOLVED
                    and any(
                        wid == candidate_id and key_stated(cell) for wid, cell in row.values.items()
                    )
                )
                missing = sorted(
                    row.metric
                    for row in metric_rows
                    if any(
                        wid == candidate_id and not key_stated(cell)
                        for wid, cell in row.values.items()
                    )
                )
                profile_limitations: list[str] = []
                if incomparable:
                    profile_limitations.append("incomparable_units")
                if missing:
                    profile_limitations.append("missing_metric")
                profiles.append(
                    {
                        "well_id": candidate_id,
                        "name": str(candidate.get("name", "")),
                        "candidate_status": "included",
                        "basis": {
                            "anchor": anchor_id,
                            "shared_problem_types": sorted(
                                set(candidate.get("shared_problem_types") or [])
                            ),
                            "shared_hole_sizes": sorted(
                                set(candidate.get("shared_hole_sizes") or [])
                            ),
                            "overlap_with_anchor": sorted(
                                anchor_types
                                & set(subject.sections["operations"].problems.get("by_type") or {})
                            ),
                        },
                        "comparable": comparable,
                        "incomparable": incomparable,
                        "missing": missing,
                        "unresolved": unresolved,
                        "limitations": profile_limitations,
                    }
                )

        basis = ComparisonBasis(
            kind=kind,
            anchor=(anchor_id or None),
            anchor_name=(rows[anchor_id].name if anchor_id and anchor_id in rows else None),
            subjects=tuple(identities),
            discovered=tuple(discovered),
            profiles=tuple(profiles),
            shared_problem_types=shared_types,
            shared_hole_sizes=shared_holes,
            same_field=(len(field_ids) == 1),
            offset_limit=(offset_limit if kind == "offset_candidates" else None),
            offset_returned=(len(discovered) if kind == "offset_candidates" else None),
            offset_at_limit=(
                len(discovered) >= offset_limit if kind == "offset_candidates" else False
            ),
            profiles_truncated=profiles_truncated,
            since=_iso(since),
            until=_iso(until),
            window_applied=bool(_iso(since) or _iso(until)),
        )

        # -- evidence ---------------------------------------------------------------------
        evidence: list[EvidenceRef] = []
        for subject in subjects:
            evidence.extend(subject.evidence)
        evidence.extend(
            self._operations_evidence(
                subjects, since=since, until=until, evidence_limit=evidence_limit
            )
        )
        evidence.extend(self._conflict_evidence(subjects, evidence_limit=evidence_limit))

        # -- limitations ------------------------------------------------------------------
        limitations: list[str] = []
        for subject in subjects:
            per_subject = decision._limitations(
                execution=subject.sections["execution"],
                operations=subject.sections["operations"],
                economics=subject.sections["economics"],
                risk=subject.sections["risk"],
                recommendations=subject.sections["recommendations"],
                patterns=subject.sections["patterns"],
                calculations=subject.sections["calculations"],
                evidence=subject.evidence,
            )
            for item in per_subject:
                if item not in limitations:
                    limitations.append(item)

        rows_flat = [row for section in sections for row in section.metrics]
        currencies = sorted(
            {
                code
                for subject in subjects
                for code in (subject.sections["economics"].summary.get("by_currency") or {})
            }
        )
        stated_presence = any(
            any(key_stated(cell) for cell in row.values.values()) for row in rows_flat
        )
        cost_rows_partial = any(
            not any(key_stated(cell) for cell in row.values.values())
            for row in rows_flat
            if row.metric.startswith("cost.")
        )
        if (len(currencies) > 1 or (bool(currencies) and cost_rows_partial)) and (
            "mixed_currency" not in limitations
        ):
            limitations.append("mixed_currency")
        if any(row.comparability == INCOMPARABLE for row in rows_flat):
            limitations.append("incomparable_units")
        if any(
            row.comparability == MISSING and any(key_stated(cell) for cell in row.values.values())
            for row in rows_flat
        ):
            limitations.append("missing_metric")
        if any(subject.conflicts for subject in subjects):
            limitations.append("unresolved_conflict")
        if not shared_types and not shared_holes:
            limitations.append("insufficient_shared_basis")
        if (
            profiles_truncated
            or evidence_limit == 0
            or detail_value == 0
            or any(ref.truncated for ref in evidence)
        ):
            limitations.append("truncated_detail")
        # deterministic de-duplication, first occurrence wins
        limitations = list(dict.fromkeys(limitations))

        # -- freshness --------------------------------------------------------------------
        freshness: dict[str, str] = {}
        section_keys = (
            "execution",
            "operations",
            "economics",
            "risk",
            "learning",
            "recommendations",
            "patterns",
            "calculations",
        )
        for key in section_keys:
            freshness[key] = _worst_freshness(
                [subject.freshness.get(key, FRESH_NOT_AVAILABLE) for subject in subjects]
            )
        freshness["conflicts"] = (
            FRESH_UNRESOLVED if any(subject.conflicts for subject in subjects) else FRESH_CURRENT
        )
        freshness["basis"] = FRESH_CURRENT

        # -- observations -----------------------------------------------------------------
        observations = self._observations(
            subjects=subjects,
            basis=basis,
            sections=sections,
            since=since,
            until=until,
            stated_presence=stated_presence,
        )

        summary = {
            "subjects": len(subjects),
            "names": [identity.name for identity in identities],
            "basis_kind": kind,
            "shared_problem_types": len(shared_types),
            "shared_hole_sizes": len(shared_holes),
            "metrics": len(rows_flat),
            "comparable": sum(1 for row in rows_flat if row.comparability == COMPARABLE),
            "incomparable": sum(1 for row in rows_flat if row.comparability == INCOMPARABLE),
            "missing": sum(1 for row in rows_flat if row.comparability == MISSING),
            "unresolved": sum(1 for row in rows_flat if row.comparability == UNRESOLVED),
            "open_conflicts": sum(len(subject.conflicts) for subject in subjects),
            "limitations": len(limitations),
        }

        request = ComparisonRequest(
            well_ids=tuple(str(value).strip() for value in well_ids if str(value).strip()),
            anchor=(str(anchor).strip() or ""),
            offsets=tuple(str(value).strip() for value in offsets if str(value).strip()),
            since=since,
            until=until,
            detail=detail_value,
            evidence_limit=evidence_limit,
            offset_limit=offset_limit,
        )

        core = ComparisonPack(
            schema=COMPARISON_SCHEMA,
            request=request,
            basis=basis,
            sections=sections,
            evidence=tuple(evidence),
            limitations=tuple(limitations),
            freshness=freshness,
            observations=tuple(observations),
            summary=summary,
            identity="",  # filled below from the payload itself
        )
        identity_hash = sha256_obj(
            {
                "schema": core.schema,
                "request": core.request.payload(),
                "basis": core.basis.payload(),
                "sections": [section.payload() for section in core.sections],
                "limitations": list(core.limitations),
                "freshness": dict(core.freshness),
                "observations": list(core.observations),
                "summary": dict(core.summary),
            }
        )
        return ComparisonPack(
            schema=core.schema,
            request=core.request,
            basis=core.basis,
            sections=core.sections,
            evidence=core.evidence,
            limitations=core.limitations,
            freshness=core.freshness,
            observations=core.observations,
            summary=core.summary,
            identity=str(identity_hash),
        )

    # -- observations ----------------------------------------------------------------------

    @staticmethod
    def _observations(
        *,
        subjects: Sequence[_SubjectData],
        basis: ComparisonBasis,
        sections: Sequence[ComparisonSection],
        since: Any,
        until: Any,
        stated_presence: bool,
    ) -> list[str]:
        """Count-derived statements only, in a fixed section order.  No ranking, no cause,
        no recommendation - a sentence here must be checkable against the numbers in the
        pack itself."""
        out: list[str] = []
        names = {subject.identity.well_id: subject.identity.name for subject in subjects}

        out.append(
            f"{len(subjects)} wells on basis {basis.kind}: "
            + ", ".join(
                f"{subject.identity.name} ({subject.identity.selection})" for subject in subjects
            )
            + "."
        )
        if basis.shared_problem_types:
            out.append(
                "Problem types recorded on every subject: "
                + ", ".join(basis.shared_problem_types)
                + "."
            )
        else:
            out.append("No problem type is recorded on every subject.")
        if basis.shared_hole_sizes:
            out.append(
                "Hole sizes recorded on every subject: " + ", ".join(basis.shared_hole_sizes) + "."
            )
        else:
            out.append("No hole size is recorded on every subject.")

        by_section = {section.section: section for section in sections}

        def count_phrase(metric: str) -> str:
            row = next(
                (row for row in by_section["operations"].metrics if row.metric == metric),
                None,
            )
            if row is None:
                return ""
            return ", ".join(
                f"{names[wid]} {_as_int(cell.get('value'))}" for wid, cell in row.values.items()
            )

        out.append(f"NPT rows by well: {count_phrase('npt.rows')}.")
        out.append("Well-control rows by well: " + count_phrase("well_control.events") + ".")
        out.append("HSE incidents attached by well: " + count_phrase("hse.incidents") + ".")

        problem_part = []
        for subject in subjects:
            types = sorted(subject.sections["operations"].problems.get("by_type") or {})
            problem_part.append(f"{subject.identity.name} {', '.join(types) if types else '-'}")
        out.append("Problem types by well: " + "; ".join(problem_part) + ".")

        exec_part = []
        for subject in subjects:
            execution = subject.sections["execution"]
            exec_part.append(
                f"{subject.identity.name} {_as_int(execution.rows)} rows "
                f"({', '.join(f'{key} {_as_int(value)}' for key, value in sorted(execution.by_status.items())) or '-'})"
            )
        out.append("Plan/actual by well: " + "; ".join(exec_part) + ".")

        cost_part = []
        currencies: set[str] = set()
        for subject in subjects:
            by_currency = subject.sections["economics"].summary.get("by_currency") or {}
            currencies.update(by_currency)
            pieces = []
            for code in sorted(by_currency):
                bucket = by_currency[code] or {}
                lines = _as_int(bucket.get("lines"))
                pieces.append(f"{code} {lines} lines")
            cost_part.append(f"{subject.identity.name} {', '.join(pieces) if pieces else '-'}")
        out.append("Cost lines by well: " + "; ".join(cost_part) + ".")
        if len(currencies) > 1:
            out.append(
                "Costs are stated in " + ", ".join(sorted(currencies)) + "; no total is "
                "reported across currencies and no currency is converted."
            )

        risk_part = []
        for subject in subjects:
            risk = subject.sections["risk"]
            risk_part.append(
                f"{subject.identity.name} {_as_int(risk.current)} current "
                f"({_as_int(risk.stated.get('severity_unassessed', 0))} with no stated severity)"
            )
        out.append("Current risks by well: " + "; ".join(risk_part) + ".")

        site_rows = sum(
            _as_int(subject.sections["operations"].hse.get("site_scoped_incidents", 0))
            for subject in subjects
        )
        if site_rows:
            out.append(
                f"{site_rows} site-scoped HSE rows exist and are not attached to any well subject."
            )

        rows_flat = [row for section in sections for row in section.metrics]
        incomparable = [row.metric for row in rows_flat if row.comparability == INCOMPARABLE]
        if incomparable:
            out.append(
                "Not comparable because units differ: " + ", ".join(sorted(incomparable)) + "."
            )
        unresolved = [
            row.metric
            for row in rows_flat
            if row.comparability == UNRESOLVED
            and any(key_stated(cell) for cell in row.values.values())
        ]
        if unresolved:
            out.append(
                "Comparability unresolved (unit or scale identity missing): "
                + ", ".join(sorted(unresolved))
                + "."
            )
        partial = [
            row.metric
            for row in rows_flat
            if row.comparability == MISSING
            and any(key_stated(cell) for cell in row.values.values())
        ]
        if partial:
            out.append("Stated on some subjects only, not all: " + ", ".join(sorted(partial)) + ".")
        conflict_total = sum(len(subject.conflicts) for subject in subjects)
        if conflict_total:
            keys = sorted(
                key
                for subject in subjects
                for key in (row["lookup_key"] for row in subject.conflicts)
            )
            out.append(
                f"{conflict_total} open knowledge conflict(s) preserved unresolved "
                f"(lookup keys: {', '.join(keys)})."
            )
        if since is not None or until is not None:
            out.append(
                "Date window applied to "
                + ", ".join(WINDOWED_DOMAINS)
                + "; current-state sections are unaffected and say so in their detail."
            )
        if basis.offset_at_limit:
            out.append(
                f"Offset discovery returned {basis.offset_returned} candidates at its "
                f"limit of {basis.offset_limit}; more may exist."
            )
        if basis.profiles_truncated:
            out.append(f"Offset candidate profiles are truncated at {OFFSET_PROFILE_CAP}.")
        if not stated_presence:
            out.append("No subject states a value for any metric.")
        return out


def _planned_cell(by_currency: Mapping[str, Any], code: str) -> dict[str, Any]:
    bucket = dict(by_currency.get(code) or {})
    if not bucket or _as_int(bucket.get("planned_lines", 0)) <= 0:
        return _value_entry(None, unit=code, state=MISSING)
    return _value_entry(round(_as_float(bucket.get("planned")), 4), unit=code, state=VALUE_STATED)


def _actual_cell(by_currency: Mapping[str, Any], code: str) -> dict[str, Any]:
    bucket = dict(by_currency.get(code) or {})
    if not bucket or _as_int(bucket.get("actual_lines", 0)) <= 0:
        return _value_entry(None, unit=code, state=MISSING)
    return _value_entry(round(_as_float(bucket.get("actual")), 4), unit=code, state=VALUE_STATED)


def _domain_count(subject: _SubjectData, domain: str) -> int:
    operations = subject.sections["operations"].payload()
    key = {
        "npt_record": ("npt", "rows"),
        "problem_occurrence": ("problems", "occurrences"),
        "well_control_event": ("well_control", "events"),
        "hse_incident": ("hse", "incidents"),
    }[domain]
    return _as_int((operations.get(key[0]) or {}).get(key[1]))


__all__ = [
    "CLAIM_DERIVED",
    "CLAIM_FACT",
    "CLAIM_NOT_CHECKABLE",
    "COMPARABILITY_STATES",
    "COMPARABLE",
    "COMPARISON_SCHEMA",
    "INCOMPARABLE",
    "MISSING",
    "NOT_APPLICABLE",
    "OFFSET_PROFILE_CAP",
    "STALE",
    "UNRESOLVED",
    "WINDOWED_DOMAINS",
    "ComparisonBasis",
    "ComparisonIntelligence",
    "ComparisonPack",
    "ComparisonRequest",
    "ComparisonSection",
    "SubjectIdentity",
]
