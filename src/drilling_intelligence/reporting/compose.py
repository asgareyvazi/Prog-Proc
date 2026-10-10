"""Map a certified decision or comparison pack into an engineering report.

No database access. No second fold. Tables and exhibits copy values the source
pack already states.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .contract import (
    REPORT_SCHEMA,
    SECTION_NO_DATA,
    SECTION_NOT_APPLICABLE,
    SECTION_PRESENT,
    SECTION_UNSUPPORTED,
    ReportExhibit,
    ReportPack,
    ReportRequest,
    ReportSection,
    ReportTable,
    merge_limitations,
    with_identity,
)
from .exhibits import exhibits_from_comparison, exhibits_from_decision

_CLAIM_FACT = "FACT"
_CLAIM_DERIVED = "DERIVED"
_CLAIM_NOT_CHECKABLE = "NOT_CHECKABLE"


def _table(
    table_id: str,
    title: str,
    columns: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    *,
    note: str = "",
    column_labels: Mapping[str, str] | None = None,
) -> ReportTable:
    return ReportTable(
        table_id=table_id,
        title=title,
        columns=tuple(columns),
        rows=tuple(dict(row) for row in rows),
        note=note,
        column_labels=dict(column_labels or {}),
    )


def _kv(
    table_id: str, title: str, pairs: Sequence[tuple[str, Any, str]], *, note: str = ""
) -> ReportTable:
    return _table(
        table_id,
        title,
        ("field", "value", "note"),
        tuple({"field": field, "value": value, "note": comment} for field, value, comment in pairs),
        note=note,
    )


def _section(
    section_id: str,
    title: str,
    state: str,
    claim_kind: str,
    *,
    note: str = "",
    tables: Sequence[ReportTable] = (),
    exhibits: Sequence[str] = (),
    limitations: Sequence[str] = (),
) -> ReportSection:
    return ReportSection(
        section_id=section_id,
        title=title,
        state=state,
        claim_kind=claim_kind,
        note=note,
        tables=tuple(tables),
        exhibits=tuple(exhibits),
        limitations=tuple(limitations),
    )


def _names(subjects: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        str(subject.get("name") or subject.get("well_id") or subject.get("id") or "")
        for subject in subjects
    ]


def _labels(subjects: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    names = _names(subjects)
    labels: dict[str, str] = {}
    for subject, name in zip(subjects, names, strict=True):
        well_id = str(subject.get("well_id") or subject.get("id") or "")
        labels[well_id] = f"{name} ({well_id})" if names.count(name) > 1 else name
    return labels


def _window_note(window: Mapping[str, Any] | None, *, applied: bool, since: Any, until: Any) -> str:
    if window:
        if window.get("applied"):
            return (
                f"Date window {window.get('since') or '...'} to {window.get('until') or '...'}. "
                f"Applies to {', '.join(window.get('windowed_domains') or ())}. "
                f"{window.get('undated_behavior') or ''}"
            )
        return (
            "No date window was applied. Current-state sections are current-state reads. "
            f"{window.get('undated_behavior') or ''}"
        )
    if applied:
        return (
            f"Date window {since or '...'} to {until or '...'} is on the request. "
            "It applies only to windowed domains in the source pack "
            "(npt, problems, well_control, hse). Undated rows stay outside the window."
        )
    return (
        "No date window was applied. Plan/actual, cost, risk, learning, patterns and "
        "calculations are current-state reads and do not pretend a window filtered them."
    )


def _freshness_table(freshness: Mapping[str, Any]) -> ReportTable:
    return _table(
        "freshness",
        "Freshness by section",
        ("section", "freshness"),
        tuple({"section": key, "freshness": freshness[key]} for key in freshness),
        note="Freshness is the source pack's vocabulary. This report does not recompute it.",
    )


def _evidence_table(evidence: Sequence[Mapping[str, Any]]) -> ReportTable:
    rows = []
    for item in evidence:
        sample = item.get("sample") or []
        rows.append(
            {
                "section": item.get("section"),
                "domain": item.get("domain"),
                "kind": item.get("kind"),
                "identity": item.get("identity"),
                "count": item.get("count"),
                "sample": list(sample),
                "truncated": item.get("truncated"),
                "method": item.get("method"),
                "scope": dict(item.get("scope") or {}),
            }
        )
    note = (
        "References are copied from the source pack. A method reference is re-executable "
        "only when method and scope are both present. Sample ids are not invented."
    )
    return _table(
        "evidence",
        "Evidence references",
        (
            "section",
            "domain",
            "kind",
            "identity",
            "count",
            "sample",
            "truncated",
            "method",
            "scope",
        ),
        rows,
        note=note,
    )


def _matrix_table(comparison: Mapping[str, Any]) -> ReportTable:
    basis = comparison.get("basis") or {}
    subjects = list(basis.get("subjects") or [])
    labels = _labels(subjects)
    columns = [
        "metric",
        "label",
        *[str(subject.get("well_id")) for subject in subjects],
        "comparability",
    ]
    rows = []
    for section in comparison.get("sections") or []:
        for row in section.get("metrics") or []:
            item: dict[str, Any] = {
                "metric": row.get("metric"),
                "label": row.get("label"),
                "comparability": row.get("comparability"),
            }
            values = row.get("values") or {}
            for subject in subjects:
                well_id = str(subject.get("well_id"))
                cell = values.get(well_id) or {}
                item[well_id] = {
                    "value": cell.get("value"),
                    "unit": cell.get("unit"),
                    "value_state": cell.get("value_state"),
                    "comparability": cell.get("comparability"),
                }
            rows.append(item)
    return _table(
        "comparison-matrix",
        "Comparison matrix (authoritative)",
        columns,
        rows,
        note=(
            "This matrix is the authoritative comparison. Charts later in the report are a view "
            "of these cells. Null is absent, not zero. Units are not converted."
        ),
        column_labels=labels,
    )


def _conflict_table(comparison: Mapping[str, Any]) -> ReportTable | None:
    rows = []
    for section in comparison.get("sections") or []:
        if section.get("section") != "conflicts":
            continue
        for well_id, detail in (section.get("detail") or {}).items():
            for conflict in (detail or {}).get("conflicts") or []:
                rows.append(
                    {
                        "well_id": well_id,
                        "id": conflict.get("id"),
                        "lookup_key": conflict.get("lookup_key"),
                        "status": conflict.get("status"),
                        "conflict_well_id": conflict.get("well_id"),
                    }
                )
    if not rows:
        return None
    return _table(
        "open-conflicts",
        "Open knowledge conflicts",
        ("well_id", "id", "lookup_key", "status", "conflict_well_id"),
        rows,
        note="Conflicts are copied as recorded. This report does not choose a winner or hide a source.",
    )


def _unsupported(section_id: str, title: str, note: str) -> ReportSection:
    return _section(
        section_id,
        title,
        SECTION_UNSUPPORTED,
        _CLAIM_NOT_CHECKABLE,
        note=note,
        limitations=(
            "timeline_not_in_source_pack"
            if section_id == "timeline"
            else "depth_series_unsupported"
        ),
    )


def _finish(
    *,
    request: ReportRequest,
    title: str,
    subject: Mapping[str, Any],
    source_packs: Sequence[Mapping[str, Any]],
    sections: Sequence[ReportSection],
    exhibits: Sequence[ReportExhibit],
    evidence: Sequence[Mapping[str, Any]],
    limitations: Sequence[str],
    freshness: Mapping[str, Any],
    observations: Sequence[str],
) -> ReportPack:
    pack = ReportPack(
        schema=REPORT_SCHEMA,
        request=request,
        mode=request.mode,
        title=title,
        subject=dict(subject),
        source_packs=tuple(dict(item) for item in source_packs),
        sections=tuple(sections),
        exhibits=tuple(exhibits),
        evidence=tuple(dict(item) for item in evidence),
        limitations=merge_limitations(limitations),
        freshness={str(key): str(value) for key, value in freshness.items()},
        observations=tuple(str(item) for item in observations),
        identity="",
    )
    return with_identity(pack)


def compose_single_well(
    decision: Mapping[str, Any],
    request: ReportRequest,
    *,
    windowed_domains: Sequence[str],
    undated_behavior: str,
) -> ReportPack:
    """A one-well report from a decision-pack document."""
    subject = decision.get("subject") or {}
    name = str(subject.get("name") or subject.get("id") or "")
    operations = decision.get("operations") or {}
    execution = decision.get("execution") or {}
    economics = decision.get("economics") or {}
    risk = decision.get("risk") or {}
    learning = decision.get("learning") or {}
    recommendations = decision.get("recommendations") or {}
    patterns = decision.get("patterns") or {}
    calculations = decision.get("calculations") or {}
    summary = decision.get("summary") or {}
    freshness = decision.get("freshness") or {}
    evidence = list(decision.get("evidence") or [])
    exhibits = exhibits_from_decision(decision)
    since = request.since
    until = request.until
    applied = bool(since or until)
    window = {
        "applied": applied,
        "since": request.payload()["since"],
        "until": request.payload()["until"],
        "windowed_domains": list(windowed_domains),
        "undated_behavior": undated_behavior,
    }
    npt = operations.get("npt") or {}
    problems = operations.get("problems") or {}
    well_control = operations.get("well_control") or {}
    hse = operations.get("hse") or {}
    cost = economics.get("summary") or {}
    sections = [
        _section(
            "cover",
            "Cover",
            SECTION_PRESENT,
            _CLAIM_FACT,
            note=(
                f"Engineering report for {name}. Mode {request.mode}. "
                f"Source {decision.get('schema')} {decision.get('identity')}."
            ),
        ),
        _section(
            "scope",
            "Scope and basis",
            SECTION_PRESENT,
            _CLAIM_FACT,
            note=_window_note(window, applied=applied, since=since, until=until),
            tables=(
                _kv(
                    "scope",
                    "Scope",
                    (
                        ("mode", request.mode, "single well; not a comparison"),
                        ("well_id", subject.get("id"), ""),
                        ("name", name, ""),
                        ("field_id", subject.get("field_id"), ""),
                        ("project_id", subject.get("project_id"), ""),
                        ("schema", decision.get("schema"), "source pack"),
                        ("source_identity", decision.get("identity"), "source pack identity"),
                    ),
                ),
                _freshness_table(freshness),
            ),
        ),
        _section(
            "summary",
            "Summary",
            SECTION_PRESENT,
            _CLAIM_DERIVED,
            note="Counts and stated totals copied from the decision pack summary. Not a score.",
            tables=(
                _kv(
                    "summary",
                    "Decision summary",
                    tuple((key, summary.get(key), "source summary") for key in summary),
                ),
            ),
        ),
        _section(
            "execution",
            "Plan versus actual",
            SECTION_PRESENT,
            str(execution.get("claim_kind") or _CLAIM_DERIVED),
            note="Statuses and match modes are the source fold. Variances are not recalculated here.",
            tables=(
                _kv(
                    "execution-counts",
                    "Execution counts",
                    (
                        ("sections", execution.get("sections"), "count"),
                        ("rows", execution.get("rows"), "count"),
                        ("sections_with_target", execution.get("sections_with_target"), "count"),
                        (
                            "sections_without_target",
                            execution.get("sections_without_target"),
                            "count",
                        ),
                        (
                            "sections_without_actual",
                            execution.get("sections_without_actual"),
                            "count",
                        ),
                    ),
                ),
                _table(
                    "execution-status",
                    "Rows by status",
                    ("status", "rows"),
                    tuple(
                        {"status": key, "rows": execution.get("by_status", {}).get(key)}
                        for key in sorted(execution.get("by_status") or {})
                    ),
                    note="ON_PLAN, VARIANCE, NO_PLAN, NO_ACTUAL, NO_TARGET and INCOMPARABLE_UNITS stay distinct.",
                ),
                _table(
                    "execution-metrics",
                    "Plan/actual by metric",
                    ("metric", "detail"),
                    tuple(
                        {"metric": key, "detail": execution.get("by_metric", {}).get(key)}
                        for key in sorted(execution.get("by_metric") or {})
                    ),
                ),
            ),
        ),
        _operations_section(
            "npt", "NPT", npt, "Hours are the source total. unknown_duration is not zero-filled."
        ),
        _operations_section(
            "problems",
            "Problems",
            problems,
            "Occurrences are recorded counts. They are not probabilities.",
        ),
        _operations_section(
            "well_control",
            "Well control",
            well_control,
            "Rows and event types as recorded. Not a severity score.",
        ),
        _operations_section(
            "hse",
            "HSE",
            hse,
            "Well-scoped and site-scoped counters stay separate. Site incidents are not attached to this well.",
        ),
        _section(
            "economics",
            "Costs by currency",
            SECTION_PRESENT,
            str(economics.get("claim_kind") or _CLAIM_DERIVED),
            note="Each currency stays in its own rows. Nothing is converted or summed across currencies.",
            tables=(
                _table(
                    "cost-by-currency",
                    "Cost summary by currency",
                    (
                        "currency",
                        "planned",
                        "actual",
                        "planned_lines",
                        "actual_lines",
                        "mixed_unit_lines",
                    ),
                    tuple(
                        {
                            "currency": code,
                            "planned": (bucket or {}).get("planned"),
                            "actual": (bucket or {}).get("actual"),
                            "planned_lines": (bucket or {}).get("planned_lines"),
                            "actual_lines": (bucket or {}).get("actual_lines"),
                            "mixed_unit_lines": (bucket or {}).get("mixed_unit_lines"),
                        }
                        for code, bucket in sorted((cost.get("by_currency") or {}).items())
                    ),
                    note="A zero beside zero lines is an empty bucket, not a stated total of zero.",
                ),
            ),
        ),
        _section(
            "risk",
            "Risks as recorded",
            SECTION_PRESENT,
            str(risk.get("claim_kind") or _CLAIM_FACT),
            note="Severity bands are source-stated counts. They are not a score and not a ranking.",
            tables=(
                _kv(
                    "risk-counts",
                    "Risk counts",
                    (
                        ("current", risk.get("current"), "count"),
                        ("superseded", risk.get("superseded"), "history, not current"),
                        ("with_evidence", risk.get("with_evidence"), "count"),
                        ("without_evidence", risk.get("without_evidence"), "count"),
                    ),
                ),
                _table(
                    "risk-severity",
                    "Severity bands as recorded",
                    ("band", "count"),
                    tuple(
                        {"band": key, "count": risk.get("severity_bands", {}).get(key)}
                        for key in sorted(risk.get("severity_bands") or {})
                    ),
                ),
            ),
        ),
        _section(
            "learning",
            "Lessons and practices",
            SECTION_PRESENT,
            str(learning.get("claim_kind") or _CLAIM_FACT),
            note="Lessons and practices stay distinct. Neither is a recommendation.",
            tables=(
                _kv(
                    "lessons",
                    "Lessons",
                    tuple(
                        (key, (learning.get("lessons") or {}).get(key), "lesson")
                        for key in (learning.get("lessons") or {})
                    ),
                ),
                _kv(
                    "practices",
                    "Practices",
                    tuple(
                        (key, (learning.get("practices") or {}).get(key), "practice")
                        for key in (learning.get("practices") or {})
                    ),
                ),
            ),
        ),
        _section(
            "recommendations",
            "Recommendations",
            SECTION_PRESENT,
            str(recommendations.get("claim_kind") or _CLAIM_FACT),
            note="Recommendations are proposals, not facts, and are not turned into advice here.",
            tables=(
                _kv(
                    "recommendations",
                    "Recommendation counts",
                    (
                        ("total", recommendations.get("total"), "count"),
                        ("with_evidence", recommendations.get("with_evidence"), "count"),
                        ("without_evidence", recommendations.get("without_evidence"), "count"),
                    ),
                ),
                _table(
                    "recommendations-status",
                    "Recommendations by status",
                    ("status", "count"),
                    tuple(
                        {"status": key, "count": recommendations.get("by_status", {}).get(key)}
                        for key in sorted(recommendations.get("by_status") or {})
                    ),
                ),
            ),
        ),
        _section(
            "patterns",
            "Observed patterns",
            SECTION_PRESENT,
            str(patterns.get("claim_kind") or _CLAIM_FACT),
            note="Patterns are observed groupings. Stale is a limitation, not a prediction.",
            tables=(
                _kv(
                    "patterns",
                    "Pattern counts",
                    (
                        ("total", patterns.get("total"), "count"),
                        ("stale", patterns.get("stale"), "count of patterns past stale_at"),
                        ("with_evidence", patterns.get("with_evidence"), "count"),
                        ("without_evidence", patterns.get("without_evidence"), "count"),
                    ),
                ),
            ),
        ),
        _section(
            "calculations",
            "Stored calculations",
            SECTION_PRESENT,
            str(calculations.get("claim_kind") or _CLAIM_FACT),
            note="Stored state only. This report does not execute calculations.",
            tables=(
                _kv(
                    "calculations",
                    "Calculation counts",
                    (
                        ("total", calculations.get("total"), "count"),
                        ("current", calculations.get("current"), "revision chain, not status"),
                        ("history", calculations.get("history"), "revision chain, not status"),
                        ("inputs", calculations.get("inputs"), "count"),
                        ("subjects_resolved", calculations.get("subjects_resolved"), "count"),
                        (
                            "subjects_truncated",
                            calculations.get("subjects_truncated"),
                            "source cap",
                        ),
                    ),
                ),
                _table(
                    "calculation-dependency",
                    "Dependency states",
                    ("state", "count"),
                    tuple(
                        {"state": key, "count": calculations.get("dependency", {}).get(key)}
                        for key in sorted(calculations.get("dependency") or {})
                    ),
                ),
            ),
        ),
        _section(
            "charts",
            "Charts",
            SECTION_PRESENT if exhibits else SECTION_NO_DATA,
            _CLAIM_DERIVED,
            note=(
                "Charts map series the decision pack already carries. Scalar counts stay in the "
                "tables. A one-value bar is not produced, because it is not a comparison and must "
                "not be read as a score."
            ),
            exhibits=tuple(item.exhibit_id for item in exhibits),
        ),
        _section(
            "evidence",
            "Evidence",
            SECTION_PRESENT if evidence else SECTION_NO_DATA,
            _CLAIM_FACT,
            note="Copied from the decision pack. Not a second provenance system.",
            tables=(_evidence_table(evidence),) if evidence else (),
        ),
        _section(
            "limitations",
            "Limitations",
            SECTION_PRESENT,
            _CLAIM_FACT,
            note="Source limitations first, then the standing limits of this report.",
        ),
        _section(
            "comparison",
            "Comparison",
            SECTION_NOT_APPLICABLE,
            _CLAIM_NOT_CHECKABLE,
            note="This report is one well. A comparison is a different request.",
        ),
        _unsupported(
            "timeline",
            "Timeline",
            "The decision pack does not carry a point-level timeline. This report does not issue a second timeline query.",
        ),
        _unsupported(
            "sections",
            "Depth and section log",
            "Plan/actual section counts are in the execution section. A depth-indexed log is not in the pack and is not invented from endpoints.",
        ),
    ]
    return _finish(
        request=request,
        title=f"Engineering report — {name}",
        subject={
            "kind": "well",
            "id": subject.get("id"),
            "name": name,
            "field_id": subject.get("field_id"),
            "project_id": subject.get("project_id"),
            "basis_kind": "single_well",
            "window": window,
        },
        source_packs=(
            {
                "role": "primary",
                "schema": decision.get("schema"),
                "identity": decision.get("identity"),
            },
        ),
        sections=sections,
        exhibits=exhibits,
        evidence=evidence,
        limitations=list(decision.get("limitations") or []),
        freshness=freshness,
        observations=list(decision.get("observations") or []),
    )


def _operations_section(
    section_id: str, title: str, payload: Mapping[str, Any], note: str
) -> ReportSection:
    pairs = []
    folds = []
    for key, value in payload.items():
        if isinstance(value, Mapping):
            folds.append((key, value))
        else:
            pairs.append((key, value, "source fold"))
    tables = [_kv(f"{section_id}-fields", f"{title} fields", pairs, note=note)]
    for key, value in folds:
        tables.append(
            _table(
                f"{section_id}-{key}",
                f"{title} {key}",
                ("key", "value"),
                tuple({"key": item, "value": value[item]} for item in sorted(value)),
                note="Source fold, alphabetical key order. Not a ranking.",
            )
        )
    return _section(
        section_id, title, SECTION_PRESENT, _CLAIM_FACT, note=note, tables=tuple(tables)
    )


def compose_comparison(comparison: Mapping[str, Any], request: ReportRequest) -> ReportPack:
    """A multi-well report from a comparison-pack document."""
    basis = comparison.get("basis") or {}
    subjects = list(basis.get("subjects") or [])
    names = _names(subjects)
    exhibits = exhibits_from_comparison(comparison)
    evidence = list(comparison.get("evidence") or [])
    conflict_table = _conflict_table(comparison)
    discovered = list(basis.get("discovered") or [])
    discovered_columns = (
        "well_id",
        "name",
        "shared_problem_types",
        "shared_hole_sizes",
        "problems",
        "npt_hours",
    )
    extra_keys = sorted(
        {
            key
            for row in discovered
            if isinstance(row, Mapping)
            for key in row
            if key not in discovered_columns
        }
    )
    scope_tables = [
        _kv(
            "basis",
            "Selection basis",
            (
                ("mode", request.mode, "report request"),
                ("kind", basis.get("kind"), "source basis kind"),
                ("anchor", basis.get("anchor"), ""),
                ("anchor_name", basis.get("anchor_name"), ""),
                ("same_field", basis.get("same_field"), ""),
                (
                    "shared_problem_types",
                    list(basis.get("shared_problem_types") or []),
                    "recorded overlap",
                ),
                (
                    "shared_hole_sizes",
                    list(basis.get("shared_hole_sizes") or []),
                    "recorded overlap",
                ),
                ("offset_limit", basis.get("offset_limit"), ""),
                ("offset_returned", basis.get("offset_returned"), ""),
                ("offset_at_limit", basis.get("offset_at_limit"), ""),
                ("profiles_truncated", basis.get("profiles_truncated"), "source cap"),
                ("source_identity", comparison.get("identity"), "comparison pack identity"),
            ),
        ),
        _freshness_table(comparison.get("freshness") or {}),
    ]
    if discovered:
        scope_tables.append(
            _table(
                "discovered-offsets",
                "Discovered offset candidates",
                (*discovered_columns, *extra_keys),
                tuple(
                    {key: row.get(key) for key in (*discovered_columns, *extra_keys)}
                    for row in discovered
                ),
                note=(
                    "Field names are the source offset_candidates names "
                    "(shared_problem_types, npt_hours). They are not a score."
                ),
            )
        )
    sections = [
        _section(
            "cover",
            "Cover",
            SECTION_PRESENT,
            _CLAIM_FACT,
            note=(
                f"Engineering report for {', '.join(names)}. Mode {request.mode}. "
                f"Basis {basis.get('kind')}. Source {comparison.get('schema')} {comparison.get('identity')}."
            ),
        ),
        _section(
            "scope",
            "Scope and basis",
            SECTION_PRESENT,
            _CLAIM_FACT,
            note=_window_note(basis.get("window") or {}, applied=False, since=None, until=None)
            + (
                " Offset profiles are capped in the source pack; profiles_truncated says whether the cap bound this report."
                if basis.get("profiles_truncated")
                else ""
            ),
            tables=tuple(scope_tables),
        ),
        _section(
            "summary",
            "Summary",
            SECTION_PRESENT,
            _CLAIM_DERIVED,
            note="Summary counts are the comparison pack's own summary. Not a ranking.",
            tables=(
                _kv(
                    "summary",
                    "Comparison summary",
                    tuple(
                        (key, (comparison.get("summary") or {}).get(key), "source summary")
                        for key in (comparison.get("summary") or {})
                    ),
                ),
            ),
        ),
        _section(
            "comparison",
            "Comparison matrix",
            SECTION_PRESENT,
            _CLAIM_FACT,
            note="Authoritative. Charts below do not replace this matrix and do not reorder wells.",
            tables=(_matrix_table(comparison),),
        ),
        _section(
            "charts",
            "Charts",
            SECTION_PRESENT if exhibits else SECTION_NO_DATA,
            _CLAIM_DERIVED,
            note=(
                "Each chart is a view of one matrix row. Incomparable units are not plotted on "
                "one axis. A missing cell stays missing. Bar height is not a safety or cost ranking."
            ),
            exhibits=tuple(item.exhibit_id for item in exhibits),
        ),
    ]
    if conflict_table is not None:
        sections.append(
            _section(
                "conflicts",
                "Open conflicts",
                SECTION_PRESENT,
                _CLAIM_FACT,
                note="Open conflicts remain open.",
                tables=(conflict_table,),
            )
        )
    sections.extend(
        [
            _section(
                "evidence",
                "Evidence",
                SECTION_PRESENT if evidence else SECTION_NO_DATA,
                _CLAIM_FACT,
                note="Copied from the comparison pack.",
                tables=(_evidence_table(evidence),) if evidence else (),
            ),
            _section(
                "limitations",
                "Limitations",
                SECTION_PRESENT,
                _CLAIM_FACT,
                note="Source limitations first, then the standing limits of this report.",
            ),
            _unsupported(
                "timeline",
                "Timeline",
                "The comparison pack does not carry a point-level timeline. This report does not issue a second timeline query.",
            ),
            _unsupported(
                "sections",
                "Depth and section log",
                "No point-level depth series is in the comparison pack. Endpoints are not plotted as a log.",
            ),
        ]
    )
    return _finish(
        request=request,
        title="Engineering report — " + ", ".join(names),
        subject={
            "kind": basis.get("kind"),
            "basis_kind": basis.get("kind"),
            "mode": request.mode,
            "anchor": basis.get("anchor"),
            "anchor_name": basis.get("anchor_name"),
            "subjects": subjects,
            "shared_problem_types": list(basis.get("shared_problem_types") or []),
            "shared_hole_sizes": list(basis.get("shared_hole_sizes") or []),
            "same_field": basis.get("same_field"),
            "profiles_truncated": basis.get("profiles_truncated"),
            "offset_limit": basis.get("offset_limit"),
            "offset_returned": basis.get("offset_returned"),
            "offset_at_limit": basis.get("offset_at_limit"),
            "window": dict(basis.get("window") or {}),
        },
        source_packs=(
            {
                "role": "primary",
                "schema": comparison.get("schema"),
                "identity": comparison.get("identity"),
            },
        ),
        sections=sections,
        exhibits=exhibits,
        evidence=evidence,
        limitations=list(comparison.get("limitations") or []),
        freshness=comparison.get("freshness") or {},
        observations=list(comparison.get("observations") or []),
    )
